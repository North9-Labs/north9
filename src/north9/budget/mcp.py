"""Budget MCP server — expose budget tools to AI agents."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import Budget, BudgetExceeded

mcp = FastMCP("north9-budget")

_budget: Budget | None = None


def _get_budget() -> Budget:
    global _budget
    if _budget is None:
        db_path = os.environ.get("BUDGET_DB", str(Path.home() / ".budget" / "usage.db"))
        session_id = os.environ.get("BUDGET_SESSION", None)
        tokens_env = os.environ.get("BUDGET_TOKENS", "")
        cost_env = os.environ.get("BUDGET_COST", "")
        tokens_limit = int(tokens_env) if tokens_env.strip() else None
        cost_limit = float(cost_env) if cost_env.strip() else None
        _budget = Budget(
            db_path=db_path,
            session_id=session_id,
            tokens_limit=tokens_limit,
            cost_limit_usd=cost_limit,
        )
    return _budget


@mcp.tool()
def budget_status() -> str:
    """Check current token and cost usage against limits. Call regularly to stay within budget."""
    b = _get_budget()
    return b.status().format()


@mcp.tool()
def budget_record(tokens_in: int, tokens_out: int, model: str = "") -> str:
    """Manually record token usage for a model call.

    Use this if you know the exact token counts from a response.
    Returns updated budget status. Raises warning if over budget.
    """
    b = _get_budget()
    try:
        status = b.record(tokens_in=tokens_in, tokens_out=tokens_out, model=model)
        return status.format()
    except BudgetExceeded as exc:
        return f"WARNING: {exc}\n\n{b.status().format()}"


@mcp.tool()
def budget_set_limit(tokens: int = 0, cost_usd: float = 0.0) -> str:
    """Set budget limits for this session.

    Args:
        tokens: Max total tokens (0 = no limit)
        cost_usd: Max cost in USD (0.0 = no limit)
    """
    b = _get_budget()
    b.tokens_limit = tokens if tokens > 0 else None
    b.cost_limit_usd = cost_usd if cost_usd > 0.0 else None
    parts = []
    if b.tokens_limit:
        parts.append(f"tokens={b.tokens_limit:,}")
    else:
        parts.append("tokens=unlimited")
    if b.cost_limit_usd:
        parts.append(f"cost=${b.cost_limit_usd:.2f}")
    else:
        parts.append("cost=unlimited")
    return f"Limits set: {', '.join(parts)}\n\n{b.status().format()}"


@mcp.tool()
def budget_sessions(limit: int = 10) -> str:
    """List recent sessions with token and cost totals."""
    b = _get_budget()
    sessions = b.sessions(limit=limit)
    if not sessions:
        return "No sessions recorded yet."
    lines = ["Recent sessions:"]
    for s in sessions:
        lines.append(
            f"  {s['session_id']}  {s['tokens']:>10,} tokens  "
            f"${s['cost_usd']:.4f}  {s['calls']} calls  started={s['started']}"
        )
    return "\n".join(lines)


@mcp.tool()
def budget_reset() -> str:
    """Reset usage tracking for current session. Clears all recorded usage."""
    b = _get_budget()
    b.reset()
    return f"Session {b.session_id} reset.\n\n{b.status().format()}"


def _install() -> None:
    """Register the Budget MCP server and hooks into Claude Code settings."""
    import sys

    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        with open(settings_path) as f:
            settings: dict[str, Any] = json.load(f)
    else:
        settings = {}

    # Register MCP server
    settings.setdefault("mcpServers", {})
    settings["mcpServers"]["north9-budget"] = {
        "command": sys.executable,
        "args": ["-m", "north9.budget"],
        "env": {}
    }

    # Build the SessionStart hook script inline
    hook_script = (
        "import sqlite3, os, pathlib; "
        "db=pathlib.Path.home()/'.budget'/'usage.db'; "
        "sid=os.environ.get('BUDGET_SESSION',''); "
        "conn=sqlite3.connect(str(db)) if db.exists() else None; "
        "rows=conn.execute('SELECT tokens_in,tokens_out,cost_usd FROM usage WHERE session_id=?',(sid,)).fetchall() if conn and sid else []; "
        "conn.close() if conn else None; "
        "tokens=sum(r[0]+r[1] for r in rows); "
        "cost=sum(r[2] for r in rows); "
        "print(f'[BUDGET] Session {sid}: {tokens} tokens used, ${cost:.4f} spent')"
    )

    settings.setdefault("hooks", {})
    settings["hooks"].setdefault("SessionStart", [])
    # Remove any existing budget hook
    settings["hooks"]["SessionStart"] = [
        h for h in settings["hooks"]["SessionStart"]
        if not (isinstance(h, dict) and "budget" in str(h).lower())
    ]
    settings["hooks"]["SessionStart"].append({
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": f"python3 -c \"{hook_script}\""
            }
        ]
    })

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    # Add CLAUDE.md section
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    budget_section = """
## Budget

Track token and cost usage. Prevent runaway agent spend.

```
budget_status()                          # check current usage
budget_set_limit(tokens=100000, cost_usd=1.00)  # set hard limits
budget_record(tokens_in=500, tokens_out=1000, model="claude-sonnet-4-6")
budget_sessions()                        # history across sessions
```

Call budget_status() regularly in long-running sessions.
"""
    if claude_md.exists():
        existing = claude_md.read_text()
        if "## Budget" not in existing:
            with open(claude_md, "a") as f:
                f.write(budget_section)
    else:
        claude_md.write_text(budget_section)

    print("Budget installed successfully.")
    print(f"  MCP server registered in {settings_path}")
    print(f"  CLAUDE.md updated at {claude_md}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        _install()
    else:
        mcp.run()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="budget", description="Budget MCP server — token and cost enforcement")
    parser.add_argument("--install", action="store_true", help="Install Budget into Claude Code")
    parser.add_argument("--db", default=None, help="SQLite DB path")
    args, _ = parser.parse_known_args()
    if args.db:
        os.environ["BUDGET_DB"] = args.db
    if args.install:
        _install()
        return
    mcp.run()
