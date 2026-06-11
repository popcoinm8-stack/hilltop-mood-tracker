"""Self-signed TLS certificate generation for network mode.

When the user runs with --network, Mood Tracker binds to 0.0.0.0 and serves
HTTPS with a self-signed certificate. This module generates that certificate
on first use and stores it in data/tls/.

The self-signed approach is correct for a LAN-only desktop app: there is no
public DNS for the operator's home IP, so Let's Encrypt / public CA issuance
isn't viable. The user accepts the browser cert warning once per device.
"""

import ipaddress
import os
import socket
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# data/tls/ — next to config.json and vault.json
TLS_DIR = Path(__file__).resolve().parent.parent / "data" / "tls"
CERT_FILE = TLS_DIR / "cert.pem"
KEY_FILE = TLS_DIR / "key.pem"

_HOSTNAME = "mood-tracker.local"
_KEY_SIZE = 2048
_DAYS_VALID = 825  # Chrome's maximum for self-signed


def get_local_ip_addresses() -> list[str]:
    """Enumerate this machine's LAN and Tailscale IPv4 addresses.

    Uses a UDP socket trick (connect to a non-routable address with port 0) to
    find the default-route interface IP, then supplements with all addresses
    returned by gethostbyname_ex.  Also includes Tailscale's 100.64.0.0/10
    range so that the app is accessible over a Tailscale mesh VPN.

    Only includes RFC 1918, loopback, and Tailscale addresses so we don't
    accidentally put a public IP into a cert SAN.
    """
    ips: set[str] = set()

    # Default-route IP via UDP socket trick.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0)
            # Connect to a non-routable address — no packets are actually sent.
            s.connect(("10.255.255.1", 1))
            ips.add(s.getsockname()[0])
    except Exception:
        pass

    # All local addresses.
    try:
        _, _, addrlist = socket.gethostbyname_ex(socket.gethostname())
        for addr in addrlist:
            ips.add(addr)
    except Exception:
        pass

    # Always include loopback.
    ips.add("127.0.0.1")

    # Filter to private/loopback/Tailscale only.
    _TS_RANGE = ipaddress.IPv4Network("100.64.0.0/10")
    private: list[str] = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
            if a.is_loopback or a.is_private or a in _TS_RANGE:
                private.append(str(a))
        except ValueError:
            continue

    return sorted(set(private))


def ensure_self_signed_cert(
    hostname: str = _HOSTNAME,
    ip_san: list[str] | None = None,
    days_valid: int = _DAYS_VALID,
) -> tuple[Path, Path]:
    """Generate a self-signed TLS certificate if one doesn't exist.

    Returns (cert_path, key_path).

    The certificate includes:
    - CN = hostname (default: mood-tracker.local)
    - SANs: hostname, localhost, 127.0.0.1, and all detected LAN IPs
    - Validity: days_valid days (default 825, Chrome's max for self-signed)
    - Key usage: digitalSignature, keyEncipherment
    - Extended key usage: serverAuth
    """
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    if ip_san is None:
        ip_san = get_local_ip_addresses()

    TLS_DIR.mkdir(parents=True, exist_ok=True)

    # Generate RSA key.
    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)

    # Build subject and issuer (self-signed, so they're the same).
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mood Tracker"),
    ])

    # Build SAN list.
    san_entries: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
    ]
    for ip in ip_san:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue

    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write key first (to temp, then atomic rename).
    key_tmp = KEY_FILE.with_suffix(".tmp")
    cert_tmp = CERT_FILE.with_suffix(".tmp")

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    key_tmp.write_bytes(key_pem)
    key_tmp.replace(KEY_FILE)

    # On Unix, restrict key file permissions. On Windows, skip (NTFS ACLs differ).
    if os.name != "nt":
        os.chmod(KEY_FILE, 0o600)

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_tmp.write_bytes(cert_pem)
    cert_tmp.replace(CERT_FILE)

    return CERT_FILE, KEY_FILE


def load_cert_paths() -> tuple[Path, Path]:
    """Return (cert_path, key_path), asserting both exist.

    Call ensure_self_signed_cert() first to generate them if needed.
    """
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        raise FileNotFoundError(
            "TLS certificate not found. Run ensure_self_signed_cert() first."
        )
    return CERT_FILE, KEY_FILE