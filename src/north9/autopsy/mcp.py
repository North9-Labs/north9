"""Autopsy MCP server — analyze AI agent sessions for waste patterns."""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import analyze_lens, analyze_session

mcp = FastMCP(
    "north9-autopsy",
    instructions=(
        "Autopsy analyzes AI agent sessions to find waste: dead loops, redundant reads, "
        "ignored LLM output, always-failing tools, and token hotspots.\n\n"
        "Feed it a .prism session file or a Lens trace DB session ID. "
        "It returns findings ranked by severity with token waste estimates.\n\n"
        "Use autopsy_session() for Prism files, autopsy_lens() for Lens DB traces."
    ),
)


@mcp.tool()
def autopsy_session(path: str, model: str = "") -> str:
    """Analyze a Prism session file for waste patterns.

    Detects: dead loops (same tool called repeatedly with same result),
    always-failing tools, redundant file reads, ignored LLM output,
    and token hogs (single LLM call consuming > 20% of session).

    Args:
        path:  Path to a .prism session file.
        model: Model name for cost estimation (optional — inferred from session if available).

    Returns a full report with findings ranked by severity.
    """
    try:
        report = analyze_session(path, model=model)
        return report.format()
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error analyzing session: {e}"


@mcp.tool()
def autopsy_lens(
    session_id: str = "",
    db_path: str = "",
    last_n: int = 100,
) -> str:
    """Analyze Lens trace records for waste patterns.

    Reads from the Lens SQLite database. Pass a session_id to analyze a
    specific session, or leave blank to analyze the most recent N calls.

    Args:
        session_id: Lens session ID to analyze (leave blank for recent calls).
        db_path:    Path to Lens DB (default: ~/.lens/traces.db).
        last_n:     Number of recent calls to analyze when no session_id given.

    Returns a full report with findings ranked by severity.
    """
    try:
        report = analyze_lens(
            session_id=session_id or None,
            db_path=db_path or None,
            last_n=last_n,
        )
        return report.format()
    except FileNotFoundError as e:
        return f"Error: {e}\n\nHint: Install Lens and run `python -m north9.lens --install` first."
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error analyzing Lens traces: {e}"


@mcp.tool()
def autopsy_json(path: str, model: str = "") -> str:
    """Analyze a Prism session file and return findings as JSON.

    Use when you need to process findings programmatically.

    Args:
        path:  Path to a .prism session file.
        model: Model name for cost estimation (optional).
    """
    try:
        report = analyze_session(path, model=model)
        return json.dumps(report.to_dict(), indent=2)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Analysis failed: {e}"})


@mcp.tool()
def autopsy_compare(path_a: str, path_b: str) -> str:
    """Compare two Prism sessions — show regression or improvement in waste patterns.

    Useful for before/after comparison: run agent with old prompt vs new prompt,
    compare autopsy reports to see if waste decreased.

    Args:
        path_a: Path to the first .prism session file (baseline).
        path_b: Path to the second .prism session file (comparison).
    """
    try:
        report_a = analyze_session(path_a)
        report_b = analyze_session(path_b)
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"

    lines = [
        "Autopsy comparison",
        f"  A: {path_a}",
        f"  B: {path_b}",
        "",
    ]

    def delta(a: float, b: float, unit: str = "", lower_is_better: bool = True) -> str:
        diff = b - a
        pct = (diff / a * 100) if a else 0
        arrow = "↓" if diff < 0 else "↑"
        better = (diff < 0) == lower_is_better
        tag = "✓" if better else "✗"
        return f"{b:.1f}{unit}  ({arrow}{abs(diff):.1f}{unit} / {abs(pct):.1f}%)  {tag}"

    lines += [
        f"{'Metric':<28} {'A':>14} {'B + Δ':>30}",
        "-" * 74,
        f"{'Total tokens':<28} {report_a.total_tokens:>14,}  {delta(report_a.total_tokens, report_b.total_tokens, '')}",
        f"{'Cost USD':<28} {report_a.total_cost_usd:>14.4f}  {delta(report_a.total_cost_usd, report_b.total_cost_usd, '$')}",
        f"{'Elapsed (s)':<28} {report_a.elapsed_ms/1000:>14.2f}  {delta(report_a.elapsed_ms/1000, report_b.elapsed_ms/1000, 's')}",
        f"{'Findings':<28} {len(report_a.findings):>14}  {delta(len(report_a.findings), len(report_b.findings), '', True)}",
        f"{'Waste tokens':<28} {report_a.tokens_wasted:>14,}  {delta(report_a.tokens_wasted, report_b.tokens_wasted, '')}",
        "",
    ]

    cats_a = {f.category for f in report_a.findings}
    cats_b = {f.category for f in report_b.findings}
    resolved = cats_a - cats_b
    new_issues = cats_b - cats_a

    if resolved:
        lines.append(f"Resolved patterns: {', '.join(sorted(resolved))}")
    if new_issues:
        lines.append(f"New patterns in B: {', '.join(sorted(new_issues))}")
    if not resolved and not new_issues:
        lines.append("Same waste pattern categories in both sessions.")

    return "\n".join(lines)


def _install() -> None:
    import json
    import sys
    from pathlib import Path

    python_exe = sys.executable
    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if "mcpServers" not in settings:
        settings["mcpServers"] = {}

    settings["mcpServers"]["north9-autopsy"] = {
        "command": python_exe,
        "args": ["-m", "north9.autopsy"],
    }

    import os
    tmp = str(settings_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(settings_path))
    print(f"Registered north9-autopsy MCP server in {settings_path}")
    print("Restart Claude Code to activate.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="north9.autopsy", description="Autopsy MCP server")
    parser.add_argument("--install", action="store_true", help="Register MCP server in Claude Code")
    args, _ = parser.parse_known_args()

    if args.install:
        _install()
        return

    mcp.run()


if __name__ == "__main__":
    main()
