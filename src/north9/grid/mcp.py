"""Grid MCP server — expose grid_map, grid_run, grid_status as MCP tools."""
from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import Grid, Task

mcp = FastMCP("grid")

_client: Any = None


def _get_client() -> Any:
    """Lazy-init Anthropic client from environment."""
    global _client
    if _client is not None:
        return _client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic

        _client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return None
    return _client


@mcp.tool()
def grid_map(
    prompts_json: str,
    model: str = "claude-haiku-4-5-20251001",
    system: str = "",
    max_tokens: int = 4096,
    max_workers: int = 10,
) -> str:
    """Run multiple prompts in parallel. Returns all results when complete.

    Args:
        prompts_json: JSON array of prompt strings, e.g. ["Summarize X", "Summarize Y"]
        model: Model to use for all tasks
        system: Optional system prompt for all tasks
        max_tokens: Max tokens per response
        max_workers: Max parallel threads (default 10)

    Returns JSON with speedup stats and all outputs.
    Useful for: parallel summarization, batch analysis, fan-out research.
    """
    client = _get_client()
    if client is None:
        return "Error: ANTHROPIC_API_KEY not set"

    try:
        prompts = json.loads(prompts_json)
        if not isinstance(prompts, list):
            return "Error: prompts_json must be a JSON array"
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON in prompts_json: {e}"

    grid = Grid(client=client, max_workers=max_workers)
    result = grid.map(
        prompts=prompts,
        model=model,
        system=system or None,
        max_tokens=max_tokens,
    )
    return json.dumps(result.to_dict(), indent=2)


@mcp.tool()
def grid_run(tasks_json: str, max_workers: int = 10) -> str:
    """Run a list of tasks with per-task model/system overrides.

    tasks_json: JSON array of task objects:
    [
      {"prompt": "...", "model": "claude-opus-4-7", "system": "...", "metadata": {"key": "val"}},
      {"prompt": "...", "model": "claude-haiku-4-5-20251001"}
    ]

    Returns JSON with results, speedup stats, and per-task outputs.
    """
    client = _get_client()
    if client is None:
        return "Error: ANTHROPIC_API_KEY not set"

    try:
        raw_tasks = json.loads(tasks_json)
        if not isinstance(raw_tasks, list):
            return "Error: tasks_json must be a JSON array"
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON in tasks_json: {e}"

    import uuid

    tasks: list[Task] = []
    for i, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            return f"Error: task at index {i} must be an object"
        if "prompt" not in raw:
            return f"Error: task at index {i} missing 'prompt' field"
        tasks.append(
            Task(
                id=raw.get("id", str(uuid.uuid4())[:8]),
                prompt=raw["prompt"],
                model=raw.get("model", "claude-haiku-4-5-20251001"),
                system=raw.get("system") or None,
                max_tokens=raw.get("max_tokens", 4096),
                metadata=raw.get("metadata", {}),
            )
        )

    grid = Grid(client=client, max_workers=max_workers)
    result = grid.run(tasks)
    return json.dumps(result.to_dict(), indent=2)


@mcp.tool()
def grid_status() -> str:
    """Show Grid configuration: default model, max workers, API key status."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    key_status = f"set ({api_key[:8]}...)" if api_key else "NOT SET"
    info = {
        "api_key": key_status,
        "default_model": "claude-haiku-4-5-20251001",
        "default_max_workers": 10,
        "default_max_tokens": 4096,
        "client_initialized": _client is not None,
    }
    return json.dumps(info, indent=2)


def _install() -> None:
    """Register Grid MCP server in Claude Code settings and update CLAUDE.md."""
    import sys

    settings_paths = [
        os.path.expanduser("~/.claude/settings.json"),
    ]

    mcp_entry = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "north9.grid"],
    }

    for settings_path in settings_paths:
        if not os.path.exists(settings_path):
            continue
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}

        if "mcpServers" not in settings:
            settings["mcpServers"] = {}
        settings["mcpServers"]["north9-grid"] = mcp_entry

        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
        print(f"Registered 'grid' MCP server in {settings_path}")

    # Update CLAUDE.md
    claude_md_path = os.path.expanduser("~/.claude/CLAUDE.md")
    grid_section = """
## Grid

Run N prompts in parallel. 10 tasks at 30s each → still 30s total.
```
grid_map('["Analyze this file", "Analyze that file", "Check dependencies"]')
grid_run('[{"prompt": "...", "model": "claude-opus-4-7"}, {"prompt": "...", "model": "claude-haiku-4-5-20251001"}]')
```
Use for parallel analysis, batch summarization, fan-out research tasks.
"""

    if os.path.exists(claude_md_path):
        with open(claude_md_path) as f:
            content = f.read()
        if "## Grid" not in content:
            with open(claude_md_path, "a") as f:
                f.write(grid_section)
            print(f"Added Grid section to {claude_md_path}")
        else:
            print(f"Grid section already present in {claude_md_path}")
    else:
        with open(claude_md_path, "w") as f:
            f.write(grid_section.lstrip())
        print(f"Created {claude_md_path} with Grid section")


if __name__ == "__main__":
    mcp.run()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="grid", description="Grid MCP server — parallel agent execution")
    parser.add_argument("--install", action="store_true", help="Install Grid into Claude Code")
    args, _ = parser.parse_known_args()
    if args.install:
        _install()
        return
    mcp.run()
