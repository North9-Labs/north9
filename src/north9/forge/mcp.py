"""Forge MCP server — exposes Forge eval tools via the Model Context Protocol."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import Assertion, Suite, SuiteResult

mcp = FastMCP("forge")

_EXAMPLE_YAML = """\
name: "Example suite"
model: "claude-haiku-4-5-20251001"
system: "You are a helpful coding assistant."
cases:
  - name: "says hello"
    input: "Say hello!"
    assert:
      - contains: "hello"
      - max_tokens: 200
  - name: "writes python"
    input: "Write a one-line Python hello world"
    assert:
      - contains: "print"
      - regex: 'print\\(.*hello.*\\)'
      - not_contains: "I cannot"
"""


def _get_client() -> Any:
    """Create an Anthropic client from environment."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. "
            "Export it before running: export ANTHROPIC_API_KEY=sk-..."
        )
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic package is required to run tests: pip install anthropic"
        ) from e
    return anthropic.Anthropic(api_key=api_key)


@mcp.tool()
def forge_run(yaml_path: str, model: str = "") -> str:
    """Run a Forge test suite from a YAML file. Returns pass/fail report.

    YAML format:
      name: "My tests"
      model: "claude-haiku-4-5-20251001"
      system: "You are a helpful assistant."
      cases:
        - name: "basic response"
          input: "Say hello"
          assert:
            - contains: "hello"
            - max_tokens: 100

    Args:
        yaml_path: Path to the YAML test suite file.
        model: Optional model override (e.g. "claude-opus-4-5"). Overrides suite-level model.
    """
    try:
        client = _get_client()
    except RuntimeError as e:
        return f"ERROR: {e}"

    try:
        suite = Suite.from_yaml(yaml_path)
    except FileNotFoundError:
        return f"ERROR: File not found: {yaml_path}"
    except Exception as e:
        return f"ERROR loading YAML: {e}"

    if model:
        suite.model = model

    try:
        result: SuiteResult = suite.run(client)
    except Exception as e:
        return f"ERROR running suite: {e}"

    return result.format_report()


@mcp.tool()
def forge_check(name: str, input: str, response: str, assertions_json: str) -> str:  # noqa: A002
    """Check a single response against assertions without calling the API.

    Useful for checking tool outputs or existing responses offline.

    Args:
        name: A label for this check (used in output only).
        input: The original prompt/input that produced the response.
        response: The model response text to evaluate.
        assertions_json: JSON array of assertions, e.g.
            '[{"contains": "hello"}, {"max_tokens": 100}]'
    """
    try:
        raw_assertions = json.loads(assertions_json)
    except json.JSONDecodeError as e:
        return f"ERROR: Invalid JSON in assertions_json: {e}"

    try:
        assertions = [Assertion.from_dict(a) for a in raw_assertions]
    except (ValueError, KeyError) as e:
        return f"ERROR: Bad assertion format: {e}"

    # Estimate token count from response length (rough: ~4 chars per token)
    token_count = max(1, len(response) // 4)

    failures: list[str] = []
    for assertion in assertions:
        ok, msg = assertion.check(response, token_count)
        if not ok:
            failures.append(msg)

    if failures:
        lines = [f"FAIL: {name}"]
        for f in failures:
            lines.append(f"  → {f}")
        return "\n".join(lines)
    return f"PASS: {name} — all {len(assertions)} assertion(s) passed"


@mcp.tool()
def forge_example() -> str:
    """Return an example YAML test suite to get started.

    Copy this output to a .yaml file and run it with forge_run().
    """
    return _EXAMPLE_YAML


def _install() -> None:
    """Register the Forge MCP server with Claude Code."""
    import subprocess
    import sys

    # Register MCP server
    result = subprocess.run(
        [
            "claude",
            "mcp",
            "add",
            "forge",
            "--",
            sys.executable,
            "-m",
            "forge.mcp",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: Could not register MCP server: {result.stderr}")
    else:
        print("Forge MCP server registered.")

    # Add CLAUDE.md section
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    section = """
## Forge

Run eval tests against AI models. Define tests in YAML, get pass/fail reports.

```
forge_run("tests/agent_tests.yaml")   # run a test suite
forge_check("test name", "input", "response", '[{"contains": "expected"}]')
forge_example()   # see YAML format
```

Create YAML test files to regression-test agent behavior.
"""
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        if "## Forge" not in content:
            claude_md.write_text(content + section, encoding="utf-8")
            print(f"Added Forge section to {claude_md}")
    else:
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(section.lstrip(), encoding="utf-8")
        print(f"Created {claude_md} with Forge section")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "install":
        _install()
    else:
        mcp.run()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="forge", description="Forge MCP server — eval framework for AI agents")
    parser.add_argument("--install", action="store_true", help="Install Forge into Claude Code")
    args, _ = parser.parse_known_args()
    if args.install:
        _install()
        return
    mcp.run()
