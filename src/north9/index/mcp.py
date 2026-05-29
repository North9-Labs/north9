"""Index MCP server — persistent semantic memory for Claude Code agents.

Stores text chunks in SQLite FTS5. Search by keyword across all sessions and projects.

Quick setup:

    pip install "git+https://github.com/North9-Labs/Index.git#egg=index"
    python3 -m index --install

This registers the MCP server and installs a SessionStart hook that injects
memory stats at every session start.

── Config (env vars) ────────────────────────────────────────────────────────

    INDEX_DB    path to SQLite database (default: ~/.index/memory.db)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import Index

mcp = FastMCP(
    "index",
    instructions=(
        "Persistent semantic memory for AI agents. Store anything worth remembering — "
        "facts, summaries, code snippets, URLs, error messages — and retrieve by keyword.\n\n"
        "Use index_add() to store context. Use index_search() to recall it later. "
        "Memory persists across all sessions and projects.\n\n"
        "index_add(content, source='project/file', tags='tag1,tag2')\n"
        "index_search('what were the auth bugs?')\n"
        "index_list(source='myproject')\n"
        "index_delete('chunk_id')"
    ),
)

_idx: Index | None = None
_DB_PATH = Path(os.environ.get("INDEX_DB", Path.home() / ".index" / "memory.db"))


def _get_idx() -> Index:
    global _idx
    if _idx is None:
        _idx = Index(_DB_PATH)
    return _idx


# ══════════════════════════════════════════════════════════════════════════════
# Tools
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def index_add(content: str, source: str = "", tags: str = "") -> str:
    """Store a text chunk in persistent memory. Searchable across all sessions.

    Args:
        content: The text to store (fact, summary, code snippet, URL, etc.)
        source: Where this came from (file path, URL, project name)
        tags: Comma-separated tags for filtering (e.g. "bug,auth,critical")

    Returns chunk ID.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    chunk_id = _get_idx().add(content=content, source=source, tags=tag_list)
    return f"Stored chunk {chunk_id}"


@mcp.tool()
def index_search(query: str, k: int = 5, source: str = "") -> str:
    """Search stored memory by keywords. Returns ranked results with snippets.

    Args:
        query: Keywords to search for (BM25 full-text search)
        k: Maximum number of results to return (default: 5)
        source: Restrict search to a specific source/project
    """
    results = _get_idx().search(query=query, k=k, source=source)
    if not results:
        return "No results found."
    lines = [f"Found {len(results)} result(s) for '{query}':\n"]
    for i, r in enumerate(results, 1):
        chunk = r.chunk
        tag_str = f"  tags: {', '.join(chunk.tags)}" if chunk.tags else ""
        source_str = f"  source: {chunk.source}" if chunk.source else ""
        lines.append(f"[{i}] id={chunk.id}  score={r.score:.3f}")
        if source_str:
            lines.append(source_str)
        if tag_str:
            lines.append(tag_str)
        lines.append(f"  {r.snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


@mcp.tool()
def index_list(source: str = "", tag: str = "", limit: int = 20) -> str:
    """List stored chunks, optionally filtered by source or tag.

    Args:
        source: Filter by source/project name
        tag: Filter by tag
        limit: Maximum chunks to return (default: 20)
    """
    chunks = _get_idx().list(source=source, tag=tag, limit=limit)
    if not chunks:
        return "No chunks stored yet." if not source and not tag else "No matching chunks found."
    lines = [f"{len(chunks)} chunk(s):\n"]
    for c in chunks:
        tag_str = f" [{', '.join(c.tags)}]" if c.tags else ""
        src_str = f" — {c.source}" if c.source else ""
        preview = c.content[:80].replace("\n", " ")
        if len(c.content) > 80:
            preview += "…"
        lines.append(f"  {c.id}{src_str}{tag_str}")
        lines.append(f"    {preview}")
        lines.append(f"    {c.created_at}")
        lines.append("")
    return "\n".join(lines).rstrip()


@mcp.tool()
def index_delete(chunk_id: str) -> str:
    """Delete a chunk by ID.

    Args:
        chunk_id: The chunk ID returned by index_add or shown in index_list/index_search
    """
    deleted = _get_idx().delete(chunk_id)
    if deleted:
        return f"Deleted chunk {chunk_id}"
    return f"Chunk {chunk_id} not found."


@mcp.tool()
def index_stats() -> str:
    """Show total chunks stored and recent additions."""
    idx = _get_idx()
    total = idx.count()
    recent = idx.list(limit=5)
    lines = [f"Total chunks: {total}\n"]
    if recent:
        lines.append("Recent additions:")
        for c in recent:
            src_str = f" — {c.source}" if c.source else ""
            preview = c.content[:60].replace("\n", " ")
            if len(c.content) > 60:
                preview += "…"
            lines.append(f"  {c.id}{src_str}: {preview}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Install
# ══════════════════════════════════════════════════════════════════════════════

_SESSION_START_HOOK = '''\
#!/usr/bin/env python3
"""Index SessionStart hook — injects memory stats at session start.

Installed by: python3 -m index --install
"""
import os
import sys
from pathlib import Path

db_path = Path(os.environ.get("INDEX_DB", Path.home() / ".index" / "memory.db"))

if not db_path.exists():
    sys.exit(0)

try:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    print(f"[INDEX] Memory store ready — {count} chunks stored."
          " Use index_search() to recall context.")
except Exception:
    sys.exit(0)
'''

_CLAUDE_MD_SECTION = '''
## Index

Persistent semantic memory across sessions and projects.
```
index_add("fact or context", source="project/file", tags="tag1,tag2")
index_search("what were the auth bugs?")   # keyword search
index_list(source="myproject")            # browse stored chunks
index_delete("chunk_id")
```
Use index_add() to store anything worth remembering. index_search() to recall it later.
'''


def _install() -> None:
    python_exe = sys.executable
    print("Installing Index into Claude Code...\n")

    hooks_dir = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    session_start_path = hooks_dir / "index-session-start.py"
    session_start_path.write_text(_SESSION_START_HOOK, encoding="utf-8")
    session_start_path.chmod(0o755)
    print(f"  SessionStart hook: {session_start_path}")

    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if "mcpServers" not in settings:
        settings["mcpServers"] = {}
    settings["mcpServers"]["north9-index"] = {
        "command": python_exe,
        "args": ["-m", "north9.index"],
    }

    if "hooks" not in settings:
        settings["hooks"] = {}

    session_start = settings["hooks"].get("SessionStart", [])
    ss_cmd = f'"{python_exe}" "{session_start_path}"'
    if not any(
        h.get("command", "") == ss_cmd
        for entry in session_start
        for h in entry.get("hooks", [])
    ):
        session_start.append({"hooks": [{"type": "command", "command": ss_cmd, "timeout": 5}]})
        settings["hooks"]["SessionStart"] = session_start


    tmp = str(settings_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(settings_path))
    print(f"  Claude Code settings: {settings_path}")
    print("    index MCP server registered")
    print("    SessionStart hook (injects memory count at session start)")

    claude_md = Path.cwd() / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## Index" not in existing:
            claude_md.write_text(existing.rstrip() + "\n" + _CLAUDE_MD_SECTION, encoding="utf-8")
            print("  CLAUDE.md updated")
        else:
            print("  CLAUDE.md already has Index section (skipped)")
    else:
        claude_md.write_text(_CLAUDE_MD_SECTION.lstrip(), encoding="utf-8")
        print("  CLAUDE.md created")

    print("\nDone. Restart Claude Code to activate.")
    print(f"\nDatabase: {_DB_PATH}")
    print("Override: INDEX_DB=/path/to/memory.db python3 -m index")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="index",
        description="Index MCP server — persistent semantic memory for Claude Code",
    )
    parser.add_argument("--install", action="store_true", help="Install Index into Claude Code")
    parser.add_argument("--db", default=None, help="Path to SQLite database")
    args, _ = parser.parse_known_args()

    if args.install:
        _install()
        return

    if args.db:
        global _DB_PATH
        _DB_PATH = Path(args.db)

    mcp.run()


if __name__ == "__main__":
    main()
