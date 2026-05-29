"""Chain MCP server — expose workflow runner as MCP tools."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import ToolExecutor, Workflow

mcp = FastMCP("chain")

# All North9 suite tools that Chain can call directly
_KNOWN_TOOLS: dict[str, tuple[str, str]] = {
    # scout
    "scout_fetch":   ("scout.mcp", "scout_fetch"),
    "scout_search":  ("scout.mcp", "scout_search"),
    "scout_list":    ("scout.mcp", "scout_list"),
    # index
    "index_add":     ("index.mcp", "index_add"),
    "index_search":  ("index.mcp", "index_search"),
    "index_list":    ("index.mcp", "index_list"),
    "index_delete":  ("index.mcp", "index_delete"),
    # forge
    "forge_run":     ("forge.mcp", "forge_run"),
    "forge_check":   ("forge.mcp", "forge_check"),
    # sift
    "sift_load":     ("sift.mcp", "sift_load"),
    "sift_query":    ("sift.mcp", "sift_query"),
    "sift_sample":   ("sift.mcp", "sift_sample"),
    # lens
    "lens_stats":    ("lens.mcp", "lens_stats"),
    "lens_record":   ("lens.mcp", "lens_record"),
    # budget
    "budget_status": ("budget.mcp", "budget_status"),
    "budget_record": ("budget.mcp", "budget_record"),
    # grid
    "grid_map":      ("grid.mcp", "grid_map"),
    # north9
    "bash":          ("north9.mcp", "bash"),
    "write_file":    ("north9.mcp", "write_file"),
    "read_file":     ("north9.mcp", "read_file"),
    "memory_anchor": ("north9.mcp", "memory_anchor"),
}


def _build_executor() -> ToolExecutor:
    """Build executor from all installed North9 tools."""
    executor = ToolExecutor()
    for tool_name, (module_path, fn_name) in _KNOWN_TOOLS.items():
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, fn_name, None)
            if fn is not None:
                executor.register(tool_name, fn)
        except ImportError:
            pass
    return executor


# Global executor — rebuilt on first use to pick up installed tools
_executor: ToolExecutor | None = None


def _get_executor() -> ToolExecutor:
    global _executor
    if _executor is None:
        _executor = _build_executor()
    return _executor


@mcp.tool()
def chain_run(yaml_path: str) -> str:
    """Run a Chain workflow from a YAML file.

    The workflow calls tools registered in the executor.
    When used through Claude Code, tool calls are executed by
    the agent which has access to all installed MCP servers.

    YAML format:
      name: "my-workflow"
      steps:
        - id: step1
          tool: scout_fetch
          args:
            url: "https://example.com"
        - id: step2
          tool: index_add
          args:
            content: "{{ step1.output }}"
            source: "web"

    on_error: "stop" (default) | "continue" | "skip"
    """
    path = Path(yaml_path)
    if not path.exists():
        return f"Error: file not found: {yaml_path}"
    try:
        workflow = Workflow.from_yaml(path)
    except Exception as e:
        return f"Error parsing workflow: {e}"
    result = workflow.run(_get_executor())
    return result.format_report()


@mcp.tool()
def chain_validate(yaml_path: str) -> str:
    """Validate a workflow YAML file without running it. Checks syntax and structure."""
    path = Path(yaml_path)
    if not path.exists():
        return f"Error: file not found: {yaml_path}"
    try:
        workflow = Workflow.from_yaml(path)
    except Exception as e:
        return f"Invalid workflow: {e}"

    issues = []
    tool_names = set()
    for step in workflow.steps:
        if not step.id:
            issues.append("A step is missing an 'id'")
        if not step.tool:
            issues.append(f"Step '{step.id}' is missing a 'tool'")
        if step.id in tool_names:
            issues.append(f"Duplicate step id: '{step.id}'")
        tool_names.add(step.id)
        if step.on_error not in ("stop", "continue", "skip"):
            issues.append(
                f"Step '{step.id}' has invalid on_error value: '{step.on_error}'. "
                "Must be 'stop', 'continue', or 'skip'."
            )

    if issues:
        return "Workflow has issues:\n" + "\n".join(f"  - {i}" for i in issues)

    lines = [f"Workflow '{workflow.name}' is valid."]
    lines.append(f"  {len(workflow.steps)} step(s):")
    for step in workflow.steps:
        desc = f" — {step.description}" if step.description else ""
        lines.append(f"    [{step.id}] {step.tool}{desc}")
    return "\n".join(lines)


@mcp.tool()
def chain_example(template: str = "research") -> str:
    """Return an example workflow YAML. Templates: research, eval, data-pipeline."""
    templates: dict[str, str] = {
        "research": """\
name: "research-and-store"
steps:
  - id: fetch
    tool: scout_fetch
    args:
      url: "https://example.com"
    description: "Fetch the page"

  - id: store
    tool: index_add
    args:
      content: "{{ fetch.output }}"
      source: "example.com"
    description: "Store in memory"

  - id: search
    tool: index_search
    args:
      query: "main topic"
      k: 3
    description: "Verify it was stored"
""",
        "eval": """\
name: "eval-and-report"
steps:
  - id: run_tests
    tool: forge_run
    args:
      yaml_path: "tests/agent_tests.yaml"
    on_error: "continue"
    description: "Run eval suite"

  - id: store_results
    tool: index_add
    args:
      content: "{{ run_tests.output }}"
      source: "forge-results"
      tags: "eval,results"
    description: "Store results in memory"
""",
        "data-pipeline": """\
name: "data-pipeline"
steps:
  - id: load
    tool: sift_load
    args:
      path: "/workspace/data.csv"
    description: "Load CSV into SQLite"

  - id: analyze
    tool: sift_query
    args:
      sql: "SELECT * FROM data LIMIT 10"
    description: "Sample the data"

  - id: store_analysis
    tool: index_add
    args:
      content: "{{ analyze.output }}"
      source: "data-analysis"
    description: "Store findings"
""",
    }

    if template not in templates:
        available = ", ".join(sorted(templates))
        return f"Unknown template '{template}'. Available: {available}"

    return templates[template]


@mcp.tool()
def chain_run_dict(workflow_json: str) -> str:
    """Run a workflow defined as JSON inline (no file needed).

    workflow_json: JSON object matching the YAML workflow format.
    Useful for one-off workflows without creating a file.
    """
    try:
        data = json.loads(workflow_json)
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON: {e}"

    try:
        workflow = Workflow.from_dict(data)
    except Exception as e:
        return f"Error building workflow: {e}"

    result = workflow.run(_get_executor())
    return result.format_report()


@mcp.tool()
def chain_list_tools() -> str:
    """List all tools available to workflows in the Chain executor."""
    executor = _get_executor()
    tools = sorted(executor._registry.keys())  # noqa: SLF001
    if not tools:
        return "No tools registered. Install north9 suite packages and restart."
    lines = [f"Available tools ({len(tools)}):"]
    lines.extend(f"  {t}" for t in tools)
    return "\n".join(lines)


def _install() -> None:
    """Register Chain in Claude Code settings and CLAUDE.md."""
    import sys

    # Locate settings.json
    settings_candidates = [
        Path.home() / ".claude" / "settings.json",
        Path("/root/.claude/settings.json"),
    ]
    settings_path: Path | None = None
    for candidate in settings_candidates:
        if candidate.exists():
            settings_path = candidate
            break
    if settings_path is None:
        settings_path = settings_candidates[0]
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load or create settings
    try:
        with open(settings_path, encoding="utf-8") as f:
            settings: dict[str, Any] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}

    settings.setdefault("mcpServers", {})
    settings["mcpServers"]["north9-chain"] = {
        "command": sys.executable,
        "args": ["-m", "north9.chain"],
        "env": {},
    }

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    print(f"Registered 'chain' MCP server in {settings_path}")

    # Update CLAUDE.md
    claude_md_path = Path.home() / ".claude" / "CLAUDE.md"
    section = """
## Chain

Run multi-step workflows connecting North9 tools.
```
chain_run("workflows/research.yaml")         # run from file
chain_run_dict('{"name":"x","steps":[...]}') # run inline
chain_validate("workflows/research.yaml")    # check syntax
chain_example("research")                    # get a template
```
Steps can reference previous step outputs with {{ step_id.output }}.
on_error: "stop" (default) | "continue" | "skip"
"""
    if claude_md_path.exists():
        content = claude_md_path.read_text(encoding="utf-8")
        if "## Chain" not in content:
            with open(claude_md_path, "a", encoding="utf-8") as f:
                f.write(section)
            print(f"Added Chain section to {claude_md_path}")
        else:
            print(f"Chain section already present in {claude_md_path}")
    else:
        claude_md_path.write_text(section.lstrip(), encoding="utf-8")
        print(f"Created {claude_md_path} with Chain section")


def main() -> None:
    """Entry point for the Chain MCP server."""
    import argparse
    parser = argparse.ArgumentParser(prog="chain", description="Chain MCP server — YAML workflow runner")
    parser.add_argument("--install", action="store_true", help="Install Chain into Claude Code")
    args, _ = parser.parse_known_args()
    if args.install:
        _install()
        return
    mcp.run()


if __name__ == "__main__":
    main()
