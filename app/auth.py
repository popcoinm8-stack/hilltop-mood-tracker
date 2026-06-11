"""Authentication, session tokens, and device management for network mode.

This module provides the security layer between the public internet (or LAN)
and the Mood Tracker app. It is ONLY active when the server is run with
--network mode.

Architecture:
  - Access password: scrypt-hashed with per-installation salt, stored in
    data/auth.json. This is the network-access password, SEPARATE from the
    vault passphrase which protects encryption at rest.
  - Session tokens: HMAC-SHA256 signed (NOT JWTs). We control every byte.
    Tokens carry device_id, device_name, iat, exp, and a random nonce.
    24-hour TTL, transparently renewed on each authenticated request via the
    X-Renewed-Session response header.
  - Device whitelist: data/devices.json stores approved devices. New devices
    start as "pending" and must be approved from the desktop UI. Once approved,
    they receive session tokens.
  - Rate limiting: imported from app.ratelimit, applied on /auth/login.

Token format:
  v1.<base64url(payload_json)>.<base64url(hmac_sha256(key, "v1." + payload_b64))>

Why HMAC-SHA256 instead of JWT:
  - No external dependency (no PyJWT or python-jose needed).
  - Every byte is auditable — we control the format fully.
  - Tokens are opaque to clients; only the server validates them.
  - HMAC is simpler to reason about than JWT's many alg/claim pitfalls.

Security trade-off on localStorage vs HttpOnly cookies:
  The session token is stored in localStorage and sent as a Bearer token in
  the Authorization header. This is more vulnerable to XSS theft than an
  HttpOnly cookie, but the SPA has strict CSP (no remote scripts, no eval)
  and the alternative (HttpOnly + SameSite=Lax cookie) would require careful
  CORS handling. Given the threat model (casual LAN neighbor, not a targeted
  web attacker), localStorage is acceptable and simpler.
"""
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
import uuid
from base64 import b64decode, b64encode, urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path
from typing import Optional

from app import ratelimit

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
AUTH_FILE = DATA_DIR / "auth.json"
DEVICES_FILE = DATA_DIR / "devices.json"

# ---------------------------------------------------------------------------
# Scrypt parameters — same as vault (N=16384 is max on this Windows build)
# ---------------------------------------------------------------------------
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

# ---------------------------------------------------------------------------
# Token parameters
# ---------------------------------------------------------------------------
TOKEN_VERSION = "v1"
SESSION_TTL_SECONDS = 86400  # 24 hours
TOKEN_SECRET_BYTES = 32  # 256-bit HMAC key

# ---------------------------------------------------------------------------
# LAN IP validation
# ---------------------------------------------------------------------------
_PRIVATE_NETWORKS: list[ipaddress.IPv4Network] = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local
    ipaddress.IPv4Network("100.64.0.0/10"),   # Tailscale CGNAT range
]


def is_lan_ip(ip_str: str) -> bool:
    """Return True if ip_str is an RFC 1918, loopback, link-local, or Tailscale address.

    Tailscale assigns IPs in the 100.64.0.0/10 CGNAT range (100.64.0.0 –
    100.127.255.255). Accepting these means that when both the PC and phone
    are on the same Tailscale network, the phone can reach the app through
    the encrypted Tailscale tunnel — the connection never touches the
    public internet.

    Rejects all other public IPs, IPv6 (we don't bind to v6), and 0.0.0.0.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        # We only support IPv4 for now.
        return False
    for net in _PRIVATE_NETWORKS:
        if addr in net:
            return True
    return False


# ---------------------------------------------------------------------------
# Auth configuration (data/auth.json)
# ---------------------------------------------------------------------------

def is_auth_enabled() -> bool:
    """Check if network authentication is enabled."""
    if not AUTH_FILE.exists():
        return False
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return data.get("enabled", False)
    except (json.JSONDecodeError, OSError):
        return False


def _load_auth_config() -> dict:
    """Load the auth configuration from data/auth.json."""
    if not AUTH_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_auth_config(config: dict) -> None:
    """Atomic write of auth config to data/auth.json."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tmp.replace(AUTH_FILE)


def enable_auth(access_password: str) -> None:
    """Enable network authentication. Sets an access password and generates
    signing secrets.

    Raises ValueError if password is too short (< 10 chars).
    Raises RuntimeError if auth is already enabled.
    """
    if len(access_password) < 10:
        raise ValueError("Access password must be at least 10 characters.")
    if is_auth_enabled():
        raise RuntimeError("Network authentication is already enabled.")

    salt = secrets.token_bytes(32)
    password_hash = hashlib.scrypt(
        access_password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    session_secret = secrets.token_bytes(TOKEN_SECRET_BYTES)
    device_token_secret = secrets.token_bytes(TOKEN_SECRET_BYTES)

    config = {
        "version": 1,
        "enabled": True,
        "access_password_hash": b64encode(password_hash).decode(),
        "access_password_salt": b64encode(salt).decode(),
        "scrypt_n": SCRYPT_N,
        "scrypt_r": SCRYPT_R,
        "scrypt_p": SCRYPT_P,
        "session_secret": b64encode(session_secret).decode(),
        "device_token_secret": b64encode(device_token_secret).decode(),
    }
    _save_auth_config(config)


def verify_access_password(plaintext: str) -> bool:
    """Constant-time verification of the access password.

    Returns True if the password matches.
    """
    config = _load_auth_config()
    if not config:
        return False

    salt = b64decode(config["access_password_salt"])
    expected = b64decode(config["access_password_hash"])
    derived = hashlib.scrypt(
        plaintext.encode("utf-8"),
        salt=salt,
        n=config.get("scrypt_n", SCRYPT_N),
        r=config.get("scrypt_r", SCRYPT_R),
        p=config.get("scrypt_p", SCRYPT_P),
        dklen=SCRYPT_DKLEN,
    )
    return hmac.compare_digest(derived, expected)


def change_access_password(old_password: str, new_password: str) -> bool:
    """Change the access password. Returns True on success, False if old password wrong."""
    if not verify_access_password(old_password):
        return False
    if len(new_password) < 10:
        raise ValueError("New password must be at least 10 characters.")

    config = _load_auth_config()
    salt = secrets.token_bytes(32)
    password_hash = hashlib.scrypt(
        new_password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    config["access_password_hash"] = b64encode(password_hash).decode()
    config["access_password_salt"] = b64encode(salt).decode()
    _save_auth_config(config)

    # Revoke all devices so they must re-authenticate with the new password.
    _revoke_all_devices()
    return True


def disable_auth() -> None:
    """Disable network authentication entirely. Deletes auth.json and devices.json."""
    for f in (AUTH_FILE, DEVICES_FILE):
        if f.exists():
            f.unlink()


# ---------------------------------------------------------------------------
# Device management (data/devices.json)
# ---------------------------------------------------------------------------

def _load_devices() -> dict:
    """Load device records from data/devices.json."""
    if not DEVICES_FILE.exists():
        return {"version": 1, "devices": []}
    try:
        return json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "devices": []}


def _save_devices(data: dict) -> None:
    """Atomic write of device records."""
    DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEVICES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(DEVICES_FILE)


def add_device(name: str, ip: str, user_agent: str, device_id: str | None = None) -> dict:
    """Create a new device record with status='pending'.

    If device_id is provided, it's used as the device's id (idempotency key
    so the SPA can re-use the same device record across logins). Otherwise,
    a new UUID is generated.

    Returns the device dict (including the id).
    """
    if not device_id:
        device_id = uuid.uuid4().hex
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    device = {
        "id": device_id,
        "name": name[:100],  # truncate long names
        "ip": ip,
        "user_agent": user_agent[:500],
        "status": "pending",
        "created_at": now,
        "approved_at": None,
        "last_seen": now,
        "approved_by": None,
    }
    data = _load_devices()
    data["devices"].append(device)
    _save_devices(data)
    return device


def update_device_status(device_id: str, status: str, approved_by: str | None = None) -> bool:
    """Set a device's status. Returns True if the device was found and updated."""
    data = _load_devices()
    found = False
    for device in data["devices"]:
        if device["id"] == device_id:
            device["status"] = status
            device["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if status == "approved":
                device["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                device["approved_by"] = approved_by
            found = True
            break
    if found:
        _save_devices(data)
    return found


def find_device(device_id: str) -> Optional[dict]:
    """Find a device by ID. Returns the device dict or None."""
    data = _load_devices()
    for device in data["devices"]:
        if device["id"] == device_id:
            return device
    return None


def find_approved_device(device_id: str) -> Optional[dict]:
    """Find a device by ID if it's approved. Returns the device dict or None."""
    device = find_device(device_id)
    if device and device["status"] == "approved":
        return device
    return None


def list_devices(status_filter: str | None = None) -> list[dict]:
    """List all devices, optionally filtered by status."""
    data = _load_devices()
    if status_filter:
        return [d for d in data["devices"] if d["status"] == status_filter]
    return list(data["devices"])


def _revoke_all_devices() -> None:
    """Set all devices to 'revoked' status. Used when password changes."""
    data = _load_devices()
    for device in data["devices"]:
        device["status"] = "revoked"
    _save_devices(data)


def touch_device(device_id: str) -> None:
    """Update last_seen timestamp for a device."""
    data = _load_devices()
    for device in data["devices"]:
        if device["id"] == device_id:
            device["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            break
    _save_devices(data)


def get_or_create_device(device_id: str | None, name: str, ip: str, user_agent: str) -> dict:
    """Return an existing device by ID, or create a new pending one.

    If device_id is provided and found, update its last_seen and return it.
    If not found or not provided, create a new pending device using the
    provided device_id (or a fresh UUID if none).
    """
    if device_id:
        existing = find_device(device_id)
        if existing:
            touch_device(device_id)
            return existing
    # New device. Use the provided device_id as the record's id so the
    # client can find this record on subsequent logins.
    return add_device(name, ip, user_agent, device_id=device_id)


# ---------------------------------------------------------------------------
# Session tokens (HMAC-SHA256 signed)
# ---------------------------------------------------------------------------

def _get_signing_key() -> bytes:
    """Return the HMAC signing key from auth config."""
    config = _load_auth_config()
    return b64decode(config["session_secret"])


def _get_device_token_secret() -> bytes:
    """Return the device token signing key from auth config."""
    config = _load_auth_config()
    return b64decode(config["device_token_secret"])


def issue_session_token(device_id: str, device_name: str) -> str:
    """Create a signed session token for an approved device.

    Token format: v1.<base64url(payload_json)>.<base64url(hmac)>
    """
    now = int(time.time())
    payload = {
        "sub": device_id,
        "name": device_name,
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
        "rand": urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("="),
    }
    payload_b64 = urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode().rstrip("=")

    signing_key = _get_signing_key()
    signature = hmac.new(
        signing_key,
        f"{TOKEN_VERSION}.{payload_b64}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sig_b64 = urlsafe_b64encode(signature).decode().rstrip("=")

    return f"{TOKEN_VERSION}.{payload_b64}.{sig_b64}"


def validate_session_token(token: str) -> Optional[dict]:
    """Validate a session token. Returns the payload dict on success, None on failure.

    On success, also updates the device's last_seen timestamp.
    """
    if not token or not token.startswith(f"{TOKEN_VERSION}."):
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    _, payload_b64, sig_b64 = parts

    # Verify HMAC.
    signing_key = _get_signing_key()
    expected_sig = hmac.new(
        signing_key,
        f"{TOKEN_VERSION}.{payload_b64}".encode("utf-8"),
        hashlib.sha256,
    ).digest()

    # Pad base64url to multiple of 4.
    padded_sig_b64 = sig_b64 + "=" * (4 - len(sig_b64) % 4) if len(sig_b64) % 4 else sig_b64
    try:
        actual_sig = urlsafe_b64decode(padded_sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    # Decode payload.
    padded_payload_b64 = payload_b64 + "=" * (4 - len(payload_b64) % 4) if len(payload_b64) % 4 else payload_b64
    try:
        payload = json.loads(urlsafe_b64decode(padded_payload_b64))
    except (json.JSONDecodeError, Exception):
        return None

    # Check expiry.
    now = int(time.time())
    if payload.get("exp", 0) < now:
        return None

    # Check device is still approved.
    device = find_approved_device(payload.get("sub", ""))
    if device is None:
        return None

    # Update last_seen.
    touch_device(payload["sub"])

    return payload


# ---------------------------------------------------------------------------
# Auth status (for the SPA to check state)
# ---------------------------------------------------------------------------

def get_auth_status() -> dict:
    """Return the current auth/network status for the SPA."""
    config = _load_auth_config()
    enabled = config.get("enabled", False)
    return {
        "auth_enabled": enabled,
        "has_password": bool(config.get("access_password_hash")),
    }