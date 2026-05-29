"""Vault — encrypted secrets store for AI agents."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".vault" / "secrets.db"
_ENV_KEY = "NORTH9_VAULT_KEY"
_PBKDF2_ITERATIONS = 600_000
_LEGACY_SALT = b"vault-north9-salt"  # v0.1 hardcoded salt — kept for migration only
_LEGACY_ITERATIONS = 100_000


def _derive_key(master_key: str, salt: bytes, iterations: int = _PBKDF2_ITERATIONS) -> bytes:
    """Derive a Fernet key from the master key using PBKDF2."""
    import base64
    import hashlib

    dk = hashlib.pbkdf2_hmac("sha256", master_key.encode(), salt, iterations)
    return base64.urlsafe_b64encode(dk)


def _get_or_create_salt(conn: sqlite3.Connection) -> bytes:
    """Return the per-vault salt, creating and persisting it on first use."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)"
    )
    row = conn.execute("SELECT value FROM meta WHERE key = 'salt'").fetchone()
    if row:
        return bytes(row[0])
    salt = os.urandom(32)
    conn.execute("INSERT INTO meta VALUES ('salt', ?)", (salt,))
    conn.commit()
    return salt


def _migrate_legacy_vault(conn: sqlite3.Connection, master_key: str) -> bool:
    """Re-encrypt all secrets from v0.1 hardcoded salt to per-vault random salt.

    Returns True if migration was performed, False if vault is already on v0.2.
    Only called when a new salt is being created (no existing meta.salt row).
    """
    from cryptography.fernet import Fernet, InvalidToken

    rows = conn.execute("SELECT name, ciphertext FROM secrets").fetchall()
    if not rows:
        return False  # empty vault — nothing to migrate

    legacy_key = _derive_key(master_key, _LEGACY_SALT, _LEGACY_ITERATIONS)
    legacy_fernet = Fernet(legacy_key)

    # Test one secret with the legacy key — if it fails, vault is not v0.1
    try:
        legacy_fernet.decrypt(bytes(rows[0][1]))
    except InvalidToken:
        return False

    # All secrets readable with legacy key — re-encrypt with new salt
    new_salt = os.urandom(32)
    new_key = _derive_key(master_key, new_salt)
    new_fernet = Fernet(new_key)

    for name, ciphertext in rows:
        plaintext = legacy_fernet.decrypt(bytes(ciphertext))
        new_ciphertext = new_fernet.encrypt(plaintext)
        conn.execute(
            "UPDATE secrets SET ciphertext = ? WHERE name = ?",
            (new_ciphertext, name),
        )

    conn.execute("INSERT INTO meta VALUES ('salt', ?)", (new_salt,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_from', 'v0.1')", )
    conn.commit()
    return True


def _get_fernet(key: bytes):
    from cryptography.fernet import Fernet

    return Fernet(key)


@dataclass
class SecretMeta:
    name: str
    tags: list[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Vault:
    """Fernet-encrypted SQLite secrets store."""

    def __init__(
        self,
        db_path: str | Path = _DEFAULT_DB,
        master_key: str | None = None,
    ):
        self.db_path = Path(db_path).expanduser()
        mk = master_key or os.environ.get(_ENV_KEY)
        if not mk:
            raise ValueError(
                f"No master key provided. Set {_ENV_KEY} env var or pass master_key=..."
            )
        self._master_key = mk
        self._fernet_key: bytes | None = None
        self._conn: sqlite3.Connection | None = None
        try:
            self._init_db()
        except Exception:
            if self._conn:
                self._conn.close()
                self._conn = None
            raise

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
        return self._conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS secrets (
                name TEXT PRIMARY KEY,
                ciphertext BLOB NOT NULL,
                tags TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()

        # Check if this is a legacy v0.1 vault (has secrets but no meta table)
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not has_meta:
            # meta table absent — either new vault or v0.1 vault needing migration
            _migrate_legacy_vault(conn, self._master_key)

        # Derive key using per-vault salt (created on first use, stored in DB)
        salt = _get_or_create_salt(conn)
        self._fernet_key = _derive_key(self._master_key, salt)
        del self._master_key  # don't hold plaintext key longer than needed

    def set(self, name: str, value: str, tags: list[str] | None = None) -> None:
        f = _get_fernet(self._fernet_key)  # type: ignore[arg-type]
        ciphertext = f.encrypt(value.encode())
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO secrets (name, ciphertext, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                ciphertext = excluded.ciphertext,
                tags = excluded.tags,
                updated_at = excluded.updated_at
        """,
            (name, ciphertext, json.dumps(tags or []), now, now),
        )
        conn.commit()

    def get(self, name: str) -> str:
        conn = self._connect()
        row = conn.execute(
            "SELECT ciphertext FROM secrets WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Secret {name!r} not found")
        f = _get_fernet(self._fernet_key)  # type: ignore[arg-type]
        return f.decrypt(row[0]).decode()

    def delete(self, name: str) -> bool:
        conn = self._connect()
        cur = conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0

    def list(self, tag: str = "") -> list[SecretMeta]:
        conn = self._connect()
        if tag:
            rows = conn.execute(
                "SELECT name, tags, created_at, updated_at FROM secrets "
                "WHERE tags LIKE ? ORDER BY name",
                (f'%"{tag}"%',),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, tags, created_at, updated_at FROM secrets ORDER BY name"
            ).fetchall()
        return [
            SecretMeta(
                name=r[0],
                tags=json.loads(r[1]) if r[1] else [],
                created_at=r[2],
                updated_at=r[3],
            )
            for r in rows
        ]

    def env(self, *names: str) -> dict[str, str]:
        """Return {name: value} dict for named secrets. For subprocess env injection."""
        return {name: self.get(name) for name in names}

    def has(self, name: str) -> bool:
        conn = self._connect()
        return (
            conn.execute(
                "SELECT 1 FROM secrets WHERE name = ?", (name,)
            ).fetchone()
            is not None
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
