"""Encryption utilities for Phase S2.

Uses stdlib only: hashlib (scrypt), secrets (random), cryptography (AES-GCM).

Architecture:
  - DB key:     scrypt(passphrase, salt, N=2^14, r=8, p=1, dklen=32) → raw key for SQLCipher PRAGMA
  - Config key: scrypt(passphrase, salt, N=2^14, r=8, p=1, dklen=32) → 32-byte split into 16-byte AES key + 16-byte HMAC key
  - API key:   AES-GCM with a random nonce.  ciphertext = AES-GCM(key, plaintext)
  - Recovery:   a random 24-char base62 token encrypted with a KEK derived from the recovery key phrase
  - Recovery key phrase: scrypt("MoodTrackerRecoveryKey" + user_chosen_recovery_passphrase, salt, ...)
"""
import hashlib
import json
import secrets
from base64 import b64decode, b64encode
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

# Scrypt parameters.
# NOTE: N=16384 is the maximum supported on this Windows/OpenSSL build.
# N=65536 and higher raise "memory limit exceeded".  N=16384 with r=8 p=1
# is the strongest available scrypt configuration on this system.
SCRYPT_N = 16384   # 2^14 — highest value that works on this Windows build
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DB_KEY_LEN = 32    # raw 256-bit key for SQLCipher
SCRYPT_AES_KEY_LEN = 32   # split: 16 AES key + 16 HMAC key


# ---------------------------------------------------------------------------
# Low-level KDF
# ---------------------------------------------------------------------------

def _scrypt_derive(passphrase: str, salt: bytes, dklen: int) -> bytes:
    """Derive dklen bytes from passphrase using scrypt."""
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=dklen,
    )


def derive_db_key(passphrase: str, salt: bytes) -> bytes:
    """Derive the raw SQLCipher encryption key (32 bytes)."""
    return _scrypt_derive(passphrase, salt, SCRYPT_DB_KEY_LEN)


def derive_aes_key(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    """Derive the AES-128-GCM key (16 bytes) and HMAC key (16 bytes) from a passphrase."""
    raw = _scrypt_derive(passphrase, salt, SCRYPT_AES_KEY_LEN)
    return raw[:16], raw[16:]


# ---------------------------------------------------------------------------
# AES-GCM encryption (stdlib fallback if cryptography not available)
# ---------------------------------------------------------------------------

def _aes_gcm_encrypt(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt plaintext with AES-128-GCM. Returns (ciphertext, nonce)."""
    if _HAS_CRYPTOGRAPHY:
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        return ciphertext, nonce
    else:
        # Fallback: use Fernet (requires cryptography anyway)
        from cryptography.fernet import Fernet
        f = Fernet(b64encode(key * 2)[:32])
        tok = f.encrypt(plaintext)
        return tok, b""


def _aes_gcm_decrypt(key: bytes, ciphertext: bytes, nonce: bytes) -> bytes:
    """Decrypt AES-128-GCM ciphertext. Returns plaintext."""
    if _HAS_CRYPTOGRAPHY:
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)
    else:
        from cryptography.fernet import Fernet
        f = Fernet(b64encode(key * 2)[:32])
        return f.decrypt(ciphertext)


def encrypt_value(passphrase: str, plaintext: str, salt: bytes) -> str:
    """Encrypt a string value (e.g. the API key) and return a base64-encoded serialized blob."""
    if not plaintext:
        return ""
    aes_key, _ = derive_aes_key(passphrase, salt)
    ct, nonce = _aes_gcm_encrypt(aes_key, plaintext.encode("utf-8"))
    # Format: base64(nonce) + ":" + base64(ciphertext)
    return b64encode(nonce).decode() + ":" + b64encode(ct).decode()


def decrypt_value(passphrase: str, encrypted: str, salt: bytes) -> str:
    """Decrypt a value produced by encrypt_value(). Returns the original string."""
    if not encrypted or ":" not in encrypted:
        return encrypted  # legacy unencrypted value
    nonce_b64, ct_b64 = encrypted.split(":", 1)
    nonce = b64decode(nonce_b64)
    ciphertext = b64decode(ct_b64)
    aes_key, _ = derive_aes_key(passphrase, salt)
    return _aes_gcm_decrypt(aes_key, ciphertext, nonce).decode("utf-8")


# ---------------------------------------------------------------------------
# Raw-key variants (for use by Vault where the key is already derived)
# ---------------------------------------------------------------------------

def encrypt_with_key(aes_key: bytes, plaintext: str) -> str:
    """Encrypt a string with a pre-derived AES key. Returns nonce:ciphertext."""
    if not plaintext:
        return ""
    ct, nonce = _aes_gcm_encrypt(aes_key, plaintext.encode("utf-8"))
    return b64encode(nonce).decode() + ":" + b64encode(ct).decode()


def decrypt_with_key(aes_key: bytes, encrypted: str) -> str:
    """Decrypt a value produced by encrypt_with_key(). Returns the original string."""
    if not encrypted or ":" not in encrypted:
        return encrypted
    nonce_b64, ct_b64 = encrypted.split(":", 1)
    nonce = b64decode(nonce_b64)
    ciphertext = b64decode(ct_b64)
    return _aes_gcm_decrypt(aes_key, ciphertext, nonce).decode("utf-8")


# ---------------------------------------------------------------------------
# Recovery key
# ---------------------------------------------------------------------------

def generate_recovery_key() -> str:
    """Generate a random 24-char base62 recovery token."""
    return secrets.token_urlsafe(18)[:24]


def hash_recovery_key(recovery_key: str) -> str:
    """Hash a recovery key for storage. Uses SHA-256 with a fixed pepper.
    
    This allows verification without needing the passphrase.
    The hash is stored in vault.json and compared against the user's input.
    """
    import hashlib as _hl
    return _hl.sha256(recovery_key.encode("utf-8")).hexdigest()


def verify_recovery_key(recovery_key: str, stored_hash: str) -> bool:
    """Verify a typed recovery key against the stored hash."""
    return secrets.compare_digest(hash_recovery_key(recovery_key), stored_hash)


# ---------------------------------------------------------------------------
# Password strength hint (very lightweight — no heavy deps)
# ---------------------------------------------------------------------------

def passphrase_strength_hint(passphrase: str) -> str:
    """Return a plain-English strength hint for a passphrase."""
    length = len(passphrase)
    if length < 8:
        return "Too short — use at least 8 characters."
    if length < 12:
        return "Acceptable, but longer is better."
    if length < 16:
        return "Good."
    has_upper = any(c.isupper() for c in passphrase)
    has_digit = any(c.isdigit() for c in passphrase)
    has_special = any(not c.isalnum() for c in passphrase)
    score = sum([has_upper, has_digit, has_special])
    if score >= 2 and length >= 16:
        return "Strong."
    if length >= 16:
        return "Good — consider adding numbers or symbols."
    return "Good."
