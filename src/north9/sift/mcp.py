"""Sift MCP server — query data files with SQL."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import Sift

mcp = FastMCP(
    "sift",
    instructions=(
        "Load CSV, JSON, and JSONL files into in-memory SQLite and query them with SQL.\n\n"
        "Faster and cheaper than reading whole files with read_file().\n\n"
        "Workflow: sift_load(path) → sift_schema(table) → sift_query(sql)\n\n"
        "Only SELECT queries are allowed. LIMIT is auto-applied if missing."
    ),
)

_sift = Sift()


@mcp.tool()
def sift_load(path: str, table: str = "", limit: int = 0) -> str:
    """Load a CSV, JSON, or JSONL file into a queryable SQLite table.

    Args:
        path: File path (absolute or relative to workspace)
        table: Table name to use (default: filename stem)
        limit: Max rows to load (0 = all rows)

    Supports: .csv, .json (array or object-with-list), .jsonl, .ndjson
    """
    try:
        return _sift.load(path, table=table or None, limit=limit or None)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def sift_query(sql: str, limit: int = 100) -> str:
    """Run a SQL SELECT query against loaded tables. Returns JSON rows.

    Examples:
        SELECT * FROM sales WHERE revenue > 10000
        SELECT category, COUNT(*) as count FROM products GROUP BY category ORDER BY count DESC
        SELECT * FROM users WHERE name LIKE '%smith%'

    Only SELECT statements are allowed. LIMIT auto-applied if missing.
    """
    try:
        rows = _sift.query(sql, limit=limit)
        if not rows:
            return "[]"
        return json.dumps(rows, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def sift_tables() -> str:
    """List all loaded tables with file source and row counts."""
    try:
        tables = _sift.tables()
        if not tables:
            return "No tables loaded. Use sift_load() to load a file."
        return json.dumps(tables, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def sift_schema(table: str) -> str:
    """Show column names and types for a table."""
    try:
        cols = _sift.schema(table)
        if not cols:
            return f"Table '{table}' not found or has no columns."
        return json.dumps(cols, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def sift_sample(table: str, n: int = 5) -> str:
    """Get sample rows from a table to understand its structure."""
    try:
        rows = _sift.sample(table, n=n)
        if not rows:
            return "[]"
        return json.dumps(rows, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


_CLAUDE_MD_SECTION = """\

## Sift

Query CSV/JSON data files with SQL. Faster than reading the whole file.
```
sift_load("/workspace/data.csv")           # load into SQLite
sift_query("SELECT * FROM data LIMIT 10") # run SQL
sift_schema("data")                        # see columns
sift_sample("data")                        # preview rows
sift_tables()                              # all loaded tables
```
sift_load is idempotent — reloading the same file replaces the old table.
"""


def _install() -> None:
    python_exe = sys.executable
    print("Installing Sift into Claude Code...\n")

    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if "mcpServers" not in settings:
        settings["mcpServers"] = {}
    settings["mcpServers"]["north9-sift"] = {
        "command": python_exe,
        "args": ["-m", "north9.sift"],
    }

    tmp = str(settings_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(settings_path))
    print(f"  Sift MCP server registered in {settings_path}")

    claude_md = Path.cwd() / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## Sift" not in existing:
            claude_md.write_text(existing.rstrip() + "\n" + _CLAUDE_MD_SECTION, encoding="utf-8")
            print("  CLAUDE.md updated")
        else:
            print("  CLAUDE.md already has Sift section (skipped)")
    else:
        claude_md.write_text(_CLAUDE_MD_SECTION.lstrip(), encoding="utf-8")
        print("  CLAUDE.md created")

    print("\nDone. Restart Claude Code to activate.\n")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="sift",
        description="Sift MCP server — query data files with SQL",
    )
    parser.add_argument("--install", action="store_true", help="Install Sift into Claude Code")
    args, _ = parser.parse_known_args()
    if args.install:
        _install()
        return
    mcp.run()


if __name__ == "__main__":
    main()
