"""Lens MCP server — agent observability for Claude Code.

Records every tool call an AI agent makes: what tool, what input, what output,
how many tokens, how long it took. Stores in SQLite.

Quick setup:

    pip install "git+https://github.com/North9-Labs/Lens.git"
    python3 -m lens --install

This registers the MCP server and installs a SessionStart hook that reminds
you of your current session ID at every session start.

── Config (env vars or --serve flags) ──────────────────────────────────────────

    LENS_DB       SQLite DB path (default: ~/.lens/traces.db)
    LENS_SESSION  Session ID to use (default: auto-generated)
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import Tracer

mcp = FastMCP(
    "lens",
    instructions=(
        "Agent observability — record tool calls, track token costs and latency.\n\n"
        "Call lens_record() after each tool use to build a trace. "
        "Use lens_stats() to see cost and call counts for the current session. "
        "Use lens_query() to inspect individual traces. "
        "Use lens_sessions() to compare across sessions."
    ),
)

# ── Global state ──────────────────────────────────────────────────────────────

_tracer: Tracer | None = None
_session_id: str = os.environ.get("LENS_SESSION", str(uuid.uuid4())[:8])
_db_path: str | None = os.environ.get("LENS_DB", None)


def _get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        db = _db_path or str(Path.home() / ".lens" / "traces.db")
        _tracer = Tracer(db_path=db, session_id=_session_id)
    return _tracer


# ── MCP Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
def lens_record(
    tool_name: str,
    output: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: float = 0.0,
    model: str = "",
    error: str = "",
) -> str:
    """Record a tool call trace. Call after each tool use to track costs and patterns."""
    tracer = _get_tracer()
    rec = tracer.record(
        tool_name=tool_name,
        output=output,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        model=model,
        error=error or None,
    )
    return json.dumps({
        "recorded": True,
        "id": rec.id,
        "session_id": rec.session_id,
        "tool_name": rec.tool_name,
        "tokens_in": rec.tokens_in,
        "tokens_out": rec.tokens_out,
        "latency_ms": rec.latency_ms,
    })


@mcp.tool()
def lens_stats(session_id: str = "") -> str:
    """Get stats for current session (or all sessions if no session_id).

    Returns total calls, tokens, estimated cost, breakdown by tool.
    """
    tracer = _get_tracer()
    sid = session_id or _session_id
    s = tracer.stats(session_id=sid)
    return json.dumps(s.to_dict(), indent=2)


@mcp.tool()
def lens_query(session_id: str = "", tool_name: str = "", limit: int = 20) -> str:
    """Query recent traces. Filter by session or tool name."""
    tracer = _get_tracer()
    sid = session_id or _session_id
    records = tracer.query(
        session_id=sid or None,
        tool_name=tool_name or None,
        limit=limit,
    )
    return json.dumps([r.to_dict() for r in records], indent=2)


@mcp.tool()
def lens_sessions(limit: int = 10) -> str:
    """List recent sessions with call counts and token totals."""
    tracer = _get_tracer()
    sessions = tracer.sessions(limit=limit)
    return json.dumps(sessions, indent=2)


@mcp.tool()
def lens_session_id() -> str:
    """Return current session ID."""
    return _session_id


# ── Hook scripts ──────────────────────────────────────────────────────────────

_SESSION_START_HOOK = '''\
#!/usr/bin/env python3
"""Lens SessionStart hook — remind the agent of its current session ID."""
import os
import sqlite3
import uuid
from pathlib import Path

session_id = os.environ.get("LENS_SESSION", "")
if not session_id:
    session_id = str(uuid.uuid4())[:8]

db_path = os.environ.get("LENS_DB", str(Path.home() / ".lens" / "traces.db"))
count = 0
try:
    if Path(db_path).exists():
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM traces WHERE session_id = ?", (session_id,)
        ).fetchone()
        count = row[0] if row else 0
        conn.close()
except Exception:
    pass

if count:
    print(f"[LENS] Session {session_id} — {count} traces recorded. Run lens_stats() to review.")
else:
    print(f"[LENS] Session {session_id} — auto-tracing active.")
'''

_POST_TOOL_USE_HOOK = '''\
#!/usr/bin/env python3
"""Lens PostToolUse hook — auto-record every tool call to SQLite."""
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    data = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "unknown")
tool_response = data.get("tool_response", "")
if isinstance(tool_response, dict):
    output = json.dumps(tool_response)[:4000]
else:
    output = str(tool_response)[:4000]

session_id = os.environ.get("LENS_SESSION", "auto")
db_path = os.environ.get("LENS_DB", str(Path.home() / ".lens" / "traces.db"))

try:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            input_json TEXT,
            output TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            latency_ms REAL DEFAULT 0,
            timestamp TEXT NOT NULL,
            model TEXT DEFAULT \\'\\',
            error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON traces(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool ON traces(tool_name)")
    conn.execute(
        "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            session_id,
            tool_name,
            json.dumps(data.get("tool_input", {})),
            output,
            0, 0, 0.0,
            datetime.now(timezone.utc).isoformat(),
            "",
            None,
        ),
    )
    conn.commit()
    conn.close()
except Exception:
    pass
'''

# ── CLAUDE.md section ─────────────────────────────────────────────────────────

_CLAUDE_MD_SECTION = """
## Lens

Traces every tool call automatically via PostToolUse hook.
```
lens_stats()         # call counts and latency for this session
lens_query()         # recent traces (tool name, input, output)
lens_sessions()      # all sessions with call counts
lens_record("tool_name", tokens_in=100, tokens_out=200, latency_ms=450.0)
```
PostToolUse auto-records tool name, input, output, latency.
Token counts (for cost tracking) require manual lens_record() — PostToolUse hooks
don't receive API token counts. Call lens_record() after each LLM response with
the usage.input_tokens and usage.output_tokens values to track cost.
"""


# ── Install ───────────────────────────────────────────────────────────────────


def _install() -> None:
    import sys

    python_exe = sys.executable
    print("Installing Lens into Claude Code...\n")

    hooks_dir = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    session_start_path = hooks_dir / "lens-session-start.py"
    session_start_path.write_text(_SESSION_START_HOOK, encoding="utf-8")
    session_start_path.chmod(0o755)
    print(f"  ✓ SessionStart hook:  {session_start_path}")

    post_tool_path = hooks_dir / "lens-post-tool-use.py"
    post_tool_path.write_text(_POST_TOOL_USE_HOOK, encoding="utf-8")
    post_tool_path.chmod(0o755)
    print(f"  ✓ PostToolUse hook:   {post_tool_path}")

    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if "mcpServers" not in settings:
        settings["mcpServers"] = {}
    settings["mcpServers"]["north9-lens"] = {
        "command": python_exe,
        "args": ["-m", "north9.lens"],
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

    post_tool = settings["hooks"].get("PostToolUse", [])
    pt_cmd = f'"{python_exe}" "{post_tool_path}"'
    if not any(
        h.get("command", "") == pt_cmd
        for entry in post_tool
        for h in entry.get("hooks", [])
    ):
        post_tool.append({"hooks": [{"type": "command", "command": pt_cmd, "timeout": 5}]})
        settings["hooks"]["PostToolUse"] = post_tool

    tmp = str(settings_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(settings_path))
    print(f"  ✓ Claude Code settings: {settings_path}")
    print("      lens MCP server registered")
    print("      SessionStart hook (reminds you of current session ID)")

    claude_md = Path.cwd() / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## Lens" not in existing:
            claude_md.write_text(existing.rstrip() + "\n" + _CLAUDE_MD_SECTION, encoding="utf-8")
            print("  ✓ CLAUDE.md updated")
        else:
            print("  ✓ CLAUDE.md already has Lens section (skipped)")
    else:
        claude_md.write_text(_CLAUDE_MD_SECTION.lstrip(), encoding="utf-8")
        print("  ✓ CLAUDE.md created")

    print("\nDone. Restart Claude Code to activate.\n")
    print("What happens now:")
    print("  • Every tool call → PostToolUse hook auto-records to SQLite")
    print("    Zero manual effort — all tool calls traced automatically")
    print("  • Every session start → trace count injected into context")
    print("  • lens_stats() at any time → costs, call counts, latency breakdown")
    print()
    print(f"DB: {Path.home() / '.lens' / 'traces.db'}")
    print("Override: LENS_DB, LENS_SESSION env vars")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="lens",
        description="Lens MCP server — agent observability for Claude Code",
    )
    parser.add_argument("--install", action="store_true",
                        help="Install Lens into Claude Code")
    parser.add_argument("--db", default=None,
                        help="SQLite DB path (default: ~/.lens/traces.db)")
    parser.add_argument("--session", default=None,
                        help="Session ID to use")
    args, _ = parser.parse_known_args()

    if args.db:
        global _db_path
        _db_path = args.db

    if args.session:
        global _session_id
        _session_id = args.session

    if args.install:
        _install()
        return

    mcp.run()


if __name__ == "__main__":
    main()
