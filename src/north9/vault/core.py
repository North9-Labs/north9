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


def _derive_key(master_key: str, salt: bytes) -> bytes:
    """Derive a Fernet key from the master key using PBKDF2 with a per-vault salt."""
    import base64
    import hashlib

    dk = hashlib.pbkdf2_hmac("sha256", master_key.encode(), salt, _PBKDF2_ITERATIONS)
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
        # Derive key using per-vault salt (generated on first use, stored in DB)
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
