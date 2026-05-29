"""Index — semantic memory store for AI agents. Add text, search by meaning."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".index" / "memory.db"


@dataclass
class Chunk:
    id: str
    content: str
    source: str
    tags: list[str]
    metadata: dict[str, Any]
    created_at: str

    @classmethod
    def from_row(cls, row: tuple) -> Chunk:
        return cls(
            id=row[0],
            content=row[1],
            source=row[2] or "",
            tags=json.loads(row[3]) if row[3] else [],
            metadata=json.loads(row[4]) if row[4] else {},
            created_at=row[5],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "tags": self.tags,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


@dataclass
class SearchResult:
    chunk: Chunk
    score: float
    snippet: str  # highlighted excerpt


class Index:
    """SQLite FTS5 memory index. Add text chunks, search by keyword."""

    def __init__(self, db_path: str | Path = _DEFAULT_DB):
        self.db_path = Path(db_path).expanduser()
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
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        conn = self._connect()
        # Main chunks table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT,
                tags TEXT,
                metadata TEXT,
                created_at TEXT NOT NULL
            )
        """)
        # FTS5 virtual table for full-text search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(id UNINDEXED, content, source, tags, content=chunks, content_rowid=rowid)
        """)
        # Keep FTS in sync via triggers
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, id, content, source, tags)
                VALUES (new.rowid, new.id, new.content, new.source, new.tags);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, id, content, source, tags)
                VALUES ('delete', old.rowid, old.id, old.content, old.source, old.tags);
            END
        """)
        conn.commit()

    def add(
        self,
        content: str,
        source: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        chunk_id = str(uuid.uuid4())[:12]
        conn = self._connect()
        conn.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                content,
                source,
                json.dumps(tags or []),
                json.dumps(metadata or {}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return chunk_id

    def search(self, query: str, k: int = 5, source: str = "") -> list[SearchResult]:
        conn = self._connect()
        if source:
            rows = conn.execute(
                """
                SELECT c.id, c.content, c.source, c.tags, c.metadata, c.created_at,
                       bm25(chunks_fts) as score
                FROM chunks_fts
                JOIN chunks c ON chunks_fts.id = c.id
                WHERE chunks_fts MATCH ? AND c.source = ?
                ORDER BY score
                LIMIT ?
                """,
                (query, source, k),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.id, c.content, c.source, c.tags, c.metadata, c.created_at,
                       bm25(chunks_fts) as score
                FROM chunks_fts
                JOIN chunks c ON chunks_fts.id = c.id
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (query, k),
            ).fetchall()

        results = []
        for row in rows:
            chunk = Chunk(
                id=row[0],
                content=row[1],
                source=row[2] or "",
                tags=json.loads(row[3]) if row[3] else [],
                metadata=json.loads(row[4]) if row[4] else {},
                created_at=row[5],
            )
            score = abs(float(row[6]))  # bm25 returns negative, lower=better
            snippet = _make_snippet(chunk.content, query)
            results.append(SearchResult(chunk=chunk, score=score, snippet=snippet))
        return results

    def get(self, chunk_id: str) -> Chunk | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT id, content, source, tags, metadata, created_at FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        return Chunk.from_row(tuple(row)) if row else None

    def delete(self, chunk_id: str) -> bool:
        conn = self._connect()
        cur = conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
        conn.commit()
        return cur.rowcount > 0

    def list(self, source: str = "", tag: str = "", limit: int = 20) -> list[Chunk]:
        conn = self._connect()
        if source:
            rows = conn.execute(
                "SELECT id, content, source, tags, metadata, created_at FROM chunks "
                "WHERE source = ? ORDER BY created_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        elif tag:
            # Escape LIKE wildcards in tag to prevent incorrect matches
            escaped = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                "SELECT id, content, source, tags, metadata, created_at FROM chunks "
                "WHERE tags LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT ?",
                (f'%"{escaped}"%', limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, content, source, tags, metadata, created_at FROM chunks "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Chunk.from_row(tuple(r)) for r in rows]

    def count(self) -> int:
        conn = self._connect()
        return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _make_snippet(content: str, query: str, max_len: int = 200) -> str:
    """Extract a relevant snippet from content around query terms."""
    query_words = query.lower().split()
    content_lower = content.lower()
    best_pos = 0
    for word in query_words:
        pos = content_lower.find(word)
        if pos >= 0:
            best_pos = max(0, pos - 60)
            break
    snippet = content[best_pos : best_pos + max_len]
    if best_pos > 0:
        snippet = "…" + snippet
    if best_pos + max_len < len(content):
        snippet = snippet + "…"
    return snippet
