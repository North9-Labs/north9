"""Scout — fetch web pages and search them later."""
from __future__ import annotations

import hashlib
import sqlite3
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".scout" / "pages.db"
_DEFAULT_CHUNK_SIZE = 800   # chars per chunk
_DEFAULT_CHUNK_OVERLAP = 100


class _TextExtractor(HTMLParser):
    """Extract visible text from HTML, stripping scripts/styles/nav."""

    _SKIP_TAGS = frozenset(["script", "style", "noscript", "nav", "footer", "header", "aside"])

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._parts)


def _extract_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_text()


def _chunk_text(
    text: str,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        # Try to break on a newline or space
        if end < len(text):
            break_pos = chunk.rfind("\n")
            if break_pos < chunk_size // 2:
                break_pos = chunk.rfind(" ")
            if break_pos > 0:
                chunk = chunk[:break_pos]
                end = start + break_pos
        chunks.append(chunk.strip())
        start = end - overlap
    return [c for c in chunks if c]


@dataclass
class Page:
    url: str
    title: str
    content_hash: str
    chunk_count: int
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "chunks": self.chunk_count,
            "fetched_at": self.fetched_at,
        }


@dataclass
class SearchResult:
    chunk_id: str
    url: str
    title: str
    snippet: str
    score: float


class Scout:
    """Fetch URLs, store chunks, search by keyword."""

    def __init__(
        self,
        db_path: str | Path = _DEFAULT_DB,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP,
        timeout: int = 15,
    ):
        self.db_path = Path(db_path).expanduser()
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.timeout = timeout
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
        return self._conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                title TEXT,
                content_hash TEXT,
                chunk_count INTEGER,
                fetched_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                content TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                FOREIGN KEY(url) REFERENCES pages(url)
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(id UNINDEXED, url UNINDEXED, content, content=chunks, content_rowid=rowid)
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, id, url, content)
                VALUES (new.rowid, new.id, new.url, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, id, url, content)
                VALUES ('delete', old.rowid, old.id, old.url, old.content);
            END
        """)
        conn.commit()

    def fetch(self, url: str, force: bool = False) -> Page:
        """Fetch a URL, extract text, store chunks. Returns Page metadata.

        If force=False and the URL was already fetched, returns cached result.
        """
        if not force:
            existing = self._get_page(url)
            if existing:
                return existing

        # Fetch
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Scout/0.1 (+https://github.com/North9-Labs/Scout)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read()
        except urllib.error.URLError as e:
            raise ValueError(f"Failed to fetch {url}: {e}") from e

        # Decode
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()
        try:
            html = raw.decode(charset, errors="replace")
        except Exception:
            html = raw.decode("utf-8", errors="replace")

        # Extract title
        title = url
        title_start = html.lower().find("<title>")
        title_end = html.lower().find("</title>")
        if title_start >= 0 and title_end > title_start:
            title = html[title_start + 7 : title_end].strip()[:200]

        # Extract text
        if "<html" in html.lower() or "<body" in html.lower():
            text = _extract_text(html)
        else:
            text = html  # plain text

        # Content hash for deduplication
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

        # Store
        conn = self._connect()
        # Remove old chunks if re-fetching
        conn.execute("DELETE FROM chunks WHERE url = ?", (url,))
        conn.execute(
            "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?)",
            (url, title, content_hash, 0, datetime.now(timezone.utc).isoformat()),
        )

        chunks = _chunk_text(text, self.chunk_size, self.chunk_overlap)
        for i, chunk in enumerate(chunks):
            conn.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), url, chunk, i),
            )

        conn.execute(
            "UPDATE pages SET chunk_count = ? WHERE url = ?",
            (len(chunks), url),
        )
        conn.commit()

        return Page(
            url=url,
            title=title,
            content_hash=content_hash,
            chunk_count=len(chunks),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    def search(self, query: str, k: int = 5, url: str = "") -> list[SearchResult]:
        """Search stored content by keyword. Optionally filter by source URL."""
        conn = self._connect()
        if url:
            rows = conn.execute(
                """
                SELECT c.id, c.url, p.title, c.content, bm25(chunks_fts) as score
                FROM chunks_fts
                JOIN chunks c ON chunks_fts.id = c.id
                JOIN pages p ON c.url = p.url
                WHERE chunks_fts MATCH ? AND c.url = ?
                ORDER BY score LIMIT ?
            """,
                (query, url, k),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.id, c.url, p.title, c.content, bm25(chunks_fts) as score
                FROM chunks_fts
                JOIN chunks c ON chunks_fts.id = c.id
                JOIN pages p ON c.url = p.url
                WHERE chunks_fts MATCH ?
                ORDER BY score LIMIT ?
            """,
                (query, k),
            ).fetchall()

        return [
            SearchResult(
                chunk_id=r[0],
                url=r[1],
                title=r[2] or r[1],
                snippet=r[3][:300] + ("…" if len(r[3]) > 300 else ""),
                score=abs(float(r[4])),
            )
            for r in rows
        ]

    def list_pages(self, limit: int = 20) -> list[Page]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT url, title, content_hash, chunk_count, fetched_at FROM pages "
            "ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [Page(*r) for r in rows]

    def delete(self, url: str) -> bool:
        conn = self._connect()
        cur = conn.execute("DELETE FROM pages WHERE url = ?", (url,))
        conn.execute("DELETE FROM chunks WHERE url = ?", (url,))
        conn.commit()
        return cur.rowcount > 0

    def stats(self) -> dict[str, int]:
        conn = self._connect()
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"pages": pages, "chunks": chunks}

    def _get_page(self, url: str) -> Page | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT url, title, content_hash, chunk_count, fetched_at FROM pages WHERE url = ?",
            (url,),
        ).fetchone()
        return Page(*row) if row else None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Scout:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
