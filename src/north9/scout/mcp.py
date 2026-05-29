"""Scout MCP server — expose Scout tools to Claude agents."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import Scout

mcp = FastMCP("scout")

_scout: Scout | None = None


def _get_scout() -> Scout:
    global _scout
    if _scout is None:
        db_path = os.environ.get("SCOUT_DB", str(Path.home() / ".scout" / "pages.db"))
        _scout = Scout(db_path=db_path)
    return _scout


@mcp.tool()
def scout_fetch(url: str, force: bool = False) -> str:
    """Fetch a URL and store its content for later search.

    Extracts clean text from HTML, chunks it, stores in SQLite.
    Use force=True to re-fetch a URL that was previously stored.

    Returns page title, chunk count, and URL.
    """
    try:
        page = _get_scout().fetch(url, force=force)
        return json.dumps(
            {
                "status": "ok",
                "url": page.url,
                "title": page.title,
                "chunks": page.chunk_count,
                "fetched_at": page.fetched_at,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"status": "error", "url": url, "error": str(e)}, indent=2)


@mcp.tool()
def scout_search(query: str, k: int = 5, url: str = "") -> str:
    """Search fetched pages by keyword.

    Returns ranked snippets from matching chunks.
    Optionally filter to a specific URL with the url parameter.
    """
    try:
        results = _get_scout().search(query, k=k, url=url)
        if not results:
            return json.dumps({"status": "ok", "query": query, "results": []}, indent=2)
        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "results": [
                    {
                        "url": r.url,
                        "title": r.title,
                        "snippet": r.snippet,
                        "score": r.score,
                    }
                    for r in results
                ],
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"status": "error", "query": query, "error": str(e)}, indent=2)


@mcp.tool()
def scout_list(limit: int = 20) -> str:
    """List fetched pages with title, chunk count, and fetch time."""
    try:
        pages = _get_scout().list_pages(limit=limit)
        return json.dumps(
            {
                "status": "ok",
                "count": len(pages),
                "pages": [p.to_dict() for p in pages],
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
def scout_delete(url: str) -> str:
    """Remove a fetched page and all its chunks from storage."""
    try:
        removed = _get_scout().delete(url)
        return json.dumps(
            {"status": "ok", "url": url, "removed": removed},
            indent=2,
        )
    except Exception as e:
        return json.dumps({"status": "error", "url": url, "error": str(e)}, indent=2)


@mcp.tool()
def scout_stats() -> str:
    """Show total pages and chunks stored."""
    try:
        s = _get_scout().stats()
        return json.dumps({"status": "ok", **s}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


_CLAUDE_MD_SECTION = """
## Scout

Fetch web pages, search their content later.
```
scout_fetch("https://docs.python.org/3/library/pathlib.html")
scout_search("pathlib glob recursive")   # search stored content
scout_list()                             # all fetched pages
scout_delete("https://...")
```
scout_fetch is idempotent — re-fetching a URL returns cached result (use force=True to refresh).
"""


def _install() -> None:
    """Register Scout as an MCP server in Claude Code settings and update CLAUDE.md."""
    import sys

    settings_paths = [
        Path.home() / ".claude" / "settings.json",
        Path(".claude") / "settings.json",
    ]

    scout_entry: dict[str, Any] = {
        "command": sys.executable,
        "args": ["-m", "north9.scout"],
        "env": {},
    }

    for settings_path in settings_paths:
        if settings_path.exists():
            import json as _json

            text = settings_path.read_text()
            data: dict[str, Any] = _json.loads(text) if text.strip() else {}
            mcp_servers = data.setdefault("mcpServers", {})
            mcp_servers["scout"] = scout_entry
            settings_path.write_text(_json.dumps(data, indent=2) + "\n")
            print(f"Registered scout MCP in {settings_path}")
            break
    else:
        # Create local settings
        local = Path(".claude") / "settings.json"
        local.parent.mkdir(exist_ok=True)
        data = {"mcpServers": {"scout": scout_entry}}
        local.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Created {local} with scout MCP entry")

    # Update CLAUDE.md
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text()
        if "## Scout" not in content:
            claude_md.write_text(content + "\n" + _CLAUDE_MD_SECTION)
            print("Updated ~/.claude/CLAUDE.md with Scout section")
    else:
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(_CLAUDE_MD_SECTION)
        print("Created ~/.claude/CLAUDE.md with Scout section")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "install":
        _install()
    else:
        mcp.run()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="scout", description="Scout MCP server — web fetch and search for agents")
    parser.add_argument("--install", action="store_true", help="Install Scout into Claude Code")
    parser.add_argument("--db", default=None, help="SQLite DB path (default: ~/.scout/pages.db)")
    args, _ = parser.parse_known_args()
    if args.install:
        _install()
        return
    mcp.run()
