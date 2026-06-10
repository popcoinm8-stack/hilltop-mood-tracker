"""Vault — manages encryption state for Phase S2.

A Vault instance holds:
  - the current DB connection (plaintext after unlock)
  - the derived DB key (in memory only)
  - the derived config decryption key (in memory only)
  - the path to the DB file

When locked:
  - DB connection is None
  - Keys are None
  - All endpoints that need the DB refuse with HTTPException 401

When unlocked:
  - DB connection is live (SQLCipher)
  - Keys are in memory
  - Normal operation

No keys are ever written to disk. The unlock materials are stored in
data/vault.json: a scrypt salt, a Argon2 time cost hint (unused, for future),
and encrypted blobs for the recovery key and API key.

Vault lifecycle:
  1. load_vault() — reads data/vault.json, returns VaultState
  2. first_setup(passphrase) — derives salt+keys, encrypts API key, saves vault.json
  3. unlock(passphrase) — derives keys, opens DB, returns Vault (or raises)
  4. lock() — closes DB, clears keys
  5. change_passphrase(old, new) — re-encrypts all blobs with the new passphrase
"""
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Optional

import app.crypto as crypto
from app import crypto as _c

# Path to vault state file
VAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "vault.json"
# Path to the database (symlink or original — see migrate_* below)
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mood.db"
# Path to plaintext DB backup during migration
_migrate_backup_dir = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# VaultState — what the rest of the app sees
# ---------------------------------------------------------------------------

class VaultState:
    """Immutable snapshot of whether the vault is locked/unlocked and what's set up."""

    def __init__(
        self,
        *,
        is_setup: bool,
        is_unlocked: bool,
        has_recovery_key: bool,
        has_api_key: bool,
        db_encrypted: bool,
        db_path: Optional[str],
    ):
        self.is_setup = is_setup        # vault.json exists with a salt
        self.is_unlocked = is_unlocked  # vault is currently open
        self.has_recovery_key = has_recovery_key
        self.has_api_key = has_api_key
        self.db_encrypted = db_encrypted  # the DB file itself is encrypted
        self.db_path = db_path            # current DB path (may differ after migration)

    def to_dict(self) -> dict:
        return {
            "is_setup": self.is_setup,
            "is_unlocked": self.is_unlocked,
            "has_recovery_key": self.has_recovery_key,
            "has_api_key": self.has_api_key,
            "db_encrypted": self.db_encrypted,
            "db_path": self.db_path,
        }


# ---------------------------------------------------------------------------
# Vault — runtime encryption state
# ---------------------------------------------------------------------------

class Vault:
    """
    Manages the encryption session. Single instance in memory.

    Attributes after unlock():
      db_key  — raw 32-byte SQLCipher key (bytes)
      config_key_aes — 16-byte AES key for config decryption
      config_key_hmac — 16-byte HMAC key (reserved for future integrity)
      salt    — the vault salt (bytes)
      conn    — open sqlcipher3.Connection, or None when locked
      db_path — current path to the DB file (Path)
    """

    def __init__(self):
        self.db_key: Optional[bytes] = None
        self.config_key_aes: Optional[bytes] = None
        self.config_key_hmac: Optional[bytes] = None
        self.salt: Optional[bytes] = None
        self.conn: Optional[object] = None  # sqlcipher3.Connection after unlock
        self.db_path: Optional[Path] = None

    def is_unlocked(self) -> bool:
        return self.conn is not None

    def unlock(self, passphrase: str, db_path: Optional[Path] = None) -> None:
        """Derive keys and open the encrypted DB."""
        vault_data = load_vault_data()
        if not vault_data:
            raise ValueError("Vault not set up.")
        self.salt = b64decode(vault_data["salt"])

        # Derive DB key
        self.db_key = crypto.derive_db_key(passphrase, self.salt)

        # Derive config keys
        aes_key, hmac_key = crypto.derive_aes_key(passphrase, self.salt)
        self.config_key_aes = aes_key
        self.config_key_hmac = hmac_key

        # Open the DB
        if db_path:
            target_path = db_path
        elif vault_data.get("db_path_b64"):
            target_path = Path(b64decode(vault_data["db_path_b64"]).decode())
        else:
            target_path = DB_PATH
        self.db_path = target_path
        self.conn = _open_sqlcipher_db(target_path, self.db_key)

    def lock(self) -> None:
        """Close DB and clear keys from memory."""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self.db_key = None
        self.config_key_aes = None
        self.config_key_hmac = None
        self.salt = None
        self.db_path = None

    def decrypt_api_key(self, encrypted: str) -> str:
        """Decrypt the stored API key using the in-memory config key."""
        if not self.config_key_aes:
            raise ValueError("Vault is locked.")
        return crypto.decrypt_with_key(self.config_key_aes, encrypted)

    def decrypt_config_value(self, encrypted: str) -> str:
        """Decrypt any stored config value."""
        if not self.config_key_aes:
            raise ValueError("Vault is locked.")
        return crypto.decrypt_with_key(self.config_key_aes, encrypted)

    def encrypt_api_key(self, plaintext: str) -> str:
        """Encrypt the API key for storage in vault.json."""
        if not self.config_key_aes:
            raise ValueError("Vault is locked.")
        return crypto.encrypt_with_key(self.config_key_aes, plaintext)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_vault: Vault = Vault()


def get_vault() -> Vault:
    return _vault


def get_vault_state() -> VaultState:
    """Return a VaultState describing the current vault status."""
    vault_data = load_vault_data()
    if not vault_data:
        return VaultState(
            is_setup=False,
            is_unlocked=_vault.is_unlocked(),
            has_recovery_key=False,
            has_api_key=False,
            db_encrypted=False,
            db_path=str(DB_PATH),
        )
    return VaultState(
        is_setup=True,
        is_unlocked=_vault.is_unlocked(),
        has_recovery_key=bool(vault_data.get("encrypted_recovery_key")),
        has_api_key=bool(vault_data.get("encrypted_api_key")),
        db_encrypted=bool(vault_data.get("db_encrypted")),
        db_path=b64decode(vault_data.get("db_path_b64", "")).decode() if vault_data.get("db_path_b64") else str(DB_PATH),
    )


# ---------------------------------------------------------------------------
# Base64 helpers (no external deps)
# ---------------------------------------------------------------------------

def b64encode(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def b64decode(data: str) -> bytes:
    import base64
    return base64.b64decode(data)


# ---------------------------------------------------------------------------
# Vault persistence
# ---------------------------------------------------------------------------

def load_vault_data() -> Optional[dict]:
    """Load vault.json if it exists."""
    if not VAULT_PATH.exists():
        return None
    with open(VAULT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_vault_data(data: dict) -> None:
    """Write vault.json (atomic via rename)."""
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = VAULT_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(VAULT_PATH)


# ---------------------------------------------------------------------------
# First-time setup
# ---------------------------------------------------------------------------

def setup_vault(
    passphrase: str,
    encrypted_api_key: str = "",
    encrypted_recovery_key: str = "",
) -> None:
    """Create the vault.json from scratch.

    Generates a random salt and derives the initial keys.
    Stores the encrypted API key and recovery key.
    """
    salt = secrets.token_bytes(16)
    vault_data = {
        "version": 1,
        "salt": b64encode(salt),
        "encrypted_api_key": encrypted_api_key,
        "encrypted_recovery_key": encrypted_recovery_key,
        "db_encrypted": True,
        "db_path_b64": b64encode(str(DB_PATH).encode()),
        "scrypt_n": crypto.SCRYPT_N,
        "scrypt_r": crypto.SCRYPT_R,
        "scrypt_p": crypto.SCRYPT_P,
    }
    save_vault_data(vault_data)


def is_vault_setup() -> bool:
    return VAULT_PATH.exists()


# ---------------------------------------------------------------------------
# Unlock
# ---------------------------------------------------------------------------

def unlock_vault(passphrase: str) -> Vault:
    """Unlock the vault. Returns the Vault instance (singleton)."""
    if _vault.is_unlocked():
        return _vault
    _vault.unlock(passphrase)
    return _vault


def lock_vault() -> None:
    _vault.lock()


# ---------------------------------------------------------------------------
# Change passphrase
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Change passphrase (when old passphrase is known)
# ---------------------------------------------------------------------------

def change_passphrase(old_passphrase: str, new_passphrase: str) -> dict:
    """Re-encrypt all encrypted blobs with a new passphrase.

    This is called when the user knows their old passphrase.
    Step 1: decrypt DB to temp plaintext
    Step 2: re-encrypt DB with new key
    Step 3: verify new DB opens
    Step 4: atomic replace
    """
    import sqlcipher3

    vault_data = load_vault_data()
    if not vault_data:
        raise ValueError("Vault not set up.")

    old_salt = b64decode(vault_data["salt"])
    old_db_key = crypto.derive_db_key(old_passphrase, old_salt)
    new_salt = secrets.token_bytes(16)
    new_db_key = crypto.derive_db_key(new_passphrase, new_salt)

    # Determine DB path
    db_path_str = b64decode(vault_data["db_path_b64"]).decode() if vault_data.get("db_path_b64") else str(DB_PATH)
    db_path = Path(db_path_str)

    temp_plaintext = db_path.with_name("mood.db.plaintext.tmp")
    temp_new_encrypted = db_path.with_name("mood.db.new.tmp")

    try:
        # Step 1: decrypt old DB to temp plaintext
        # Use sqlcipher_export to detach encrypted data
        dec_conn = sqlcipher3.connect(str(temp_plaintext))
        dec_conn.execute("PRAGMA key = ''")  # empty key = plaintext
        dec_conn.execute(f"ATTACH DATABASE '{db_path}' AS src KEY '{old_db_key.hex()}'")
        dec_conn.execute("SELECT sqlcipher_export('main', 'src')")
        dec_conn.execute("DETACH DATABASE src")
        dec_conn.close()

        # Step 2: re-encrypt to temp_new_encrypted with new key
        enc_conn = sqlcipher3.connect(str(temp_new_encrypted))
        enc_conn.execute(f"PRAGMA key = '{new_db_key.hex()}'")
        enc_conn.execute(f"ATTACH DATABASE '{temp_plaintext}' AS src KEY ''")
        enc_conn.execute("SELECT sqlcipher_export('main', 'src')")
        enc_conn.execute("DETACH DATABASE src")
        enc_conn.close()

        # Step 3: verify new encrypted DB opens with new key
        verify_conn = sqlcipher3.connect(str(temp_new_encrypted))
        verify_conn.execute(f"PRAGMA key = '{new_db_key.hex()}'")
        try:
            count = verify_conn.execute("SELECT count(*) FROM entries").fetchone()
            verify_conn.close()
        except Exception as exc:
            temp_new_encrypted.unlink()
            raise ValueError(f"New encrypted DB verification failed: {exc}") from exc

        # Step 4: atomic replace
        db_path.unlink()
        temp_new_encrypted.rename(db_path)

        # Step 5: update vault.json
        enc_api_key = vault_data.get("encrypted_api_key", "")
        new_enc_api_key = enc_api_key
        if enc_api_key and ":" in enc_api_key:
            try:
                plaintext = crypto.decrypt_with_key(crypto.derive_aes_key(old_passphrase, old_salt)[0], enc_api_key)
                new_aes_key = crypto.derive_aes_key(new_passphrase, new_salt)[0]
                new_enc_api_key = crypto.encrypt_with_key(new_aes_key, plaintext)
            except Exception:
                pass  # keep old value

        # Recovery key is stored plain — just re-encrypt with new passphrase+salt
        enc_rk = vault_data.get("encrypted_recovery_key", "")
        new_enc_rk = enc_rk
        if enc_rk:
            try:
                new_aes_key = crypto.derive_aes_key(new_passphrase, new_salt)[0]
                new_enc_rk = crypto.encrypt_with_key(new_aes_key, enc_rk)
            except Exception:
                pass

        new_vault_data = dict(vault_data)
        new_vault_data["salt"] = b64encode(new_salt).decode()
        new_vault_data["encrypted_api_key"] = new_enc_api_key
        new_vault_data["encrypted_recovery_key"] = new_enc_rk
        save_vault_data(new_vault_data)

    finally:
        # Clean up temp files
        if temp_plaintext.exists():
            temp_plaintext.unlink()
        if temp_new_encrypted.exists():
            try:
                temp_new_encrypted.unlink()
            except Exception:
                pass

    return {"ok": True}


# ---------------------------------------------------------------------------
# SQLCipher helpers
# ---------------------------------------------------------------------------

def _open_sqlcipher_db(path: Path, db_key: bytes) -> object:
    """Open a SQLCipher database with the given raw key."""
    import sqlcipher3
    conn = sqlcipher3.connect(str(path))
    conn.execute(f"PRAGMA key = '{db_key.hex()}'")
    conn.execute("PRAGMA cipher_compatibility = 4")
    # Verify the key is correct by querying the schema
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as exc:
        conn.close()
        raise ValueError(f"Could not unlock database — incorrect passphrase. {exc}") from exc
    conn.row_factory = sqlcipher3.Row
    return conn


def test_sqlcipher_connection(path: Path, passphrase: str) -> bool:
    """Test if a passphrase can open a SQLCipher DB at the given path."""
    vault_data = load_vault_data()
    if not vault_data:
        return False
    salt = b64decode(vault_data["salt"])
    key = crypto.derive_db_key(passphrase, salt)
    try:
        import sqlcipher3
        conn = sqlcipher3.connect(str(path))
        conn.execute(f"PRAGMA key = x'{key.hex()}'")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

def get_migration_status() -> dict:
    """Return migration status for the UI."""
    vault_data = load_vault_data()
    if not vault_data:
        return {
            "vault_setup": False,
            "db_encrypted": False,
            "plaintext_db_exists": DB_PATH.exists(),
            "plaintext_backup_exists": False,
            "migration_needed": False,
        }
    db_path_b64 = vault_data.get("db_path_b64", "")
    current_db = b64decode(db_path_b64.encode()).decode() if db_path_b64 else str(DB_PATH)
    plaintext_backup = _migrate_backup_dir / f"mood.db.plaintext.backup"
    return {
        "vault_setup": True,
        "db_encrypted": vault_data.get("db_encrypted", False),
        "current_db_path": current_db,
        "plaintext_db_exists": DB_PATH.exists(),
        "plaintext_backup_exists": plaintext_backup.exists(),
        "migration_needed": vault_data.get("db_encrypted", False) and not _migrate_backup_dir.glob("mood.db.plaintext.backup"),
    }
