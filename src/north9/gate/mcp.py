"""Gate MCP server — manage and query policy via MCP tools."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

from .core import Policy, Rule

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_policy: Policy | None = None
_policy_path: str = str(Path.home() / ".gate" / "policy.yaml")

# ---------------------------------------------------------------------------
# PreToolUse hook script (written to disk during --install)
# ---------------------------------------------------------------------------

_HOOK_SCRIPT = '''\
#!/usr/bin/env python3
"""Gate PreToolUse hook — enforce policy on every tool call."""
import json, os, sys, re
from pathlib import Path

try:
    data = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})
policy_path = Path(os.environ.get("GATE_POLICY", str(Path.home() / ".gate" / "policy.yaml")))

if not policy_path.exists():
    sys.exit(0)

try:
    import yaml
    with open(policy_path) as f:
        policy_data = yaml.safe_load(f)
    rules = policy_data.get("rules", [])
except Exception:
    sys.exit(0)

import fnmatch

for rule in rules:
    rule_tool = rule.get("tool", "*")
    rule_match = rule.get("match", "")
    rule_decision = rule.get("decision", "block")
    rule_reason = rule.get("reason", "Policy violation")

    # Check tool pattern
    if rule_tool != "*" and not fnmatch.fnmatch(tool_name, rule_tool):
        continue

    # Check input pattern
    input_str = str(tool_input)
    try:
        if re.search(rule_match, input_str, re.IGNORECASE | re.DOTALL):
            if rule_decision == "block":
                print(json.dumps({"decision": "block", "reason": rule_reason}))
                sys.exit(2)
            # warn: print to stderr, allow
            print(f"[GATE] Warning: {rule_reason}", file=sys.stderr)
    except re.error:
        continue

sys.exit(0)
'''

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_policy() -> Policy:
    """Return in-memory policy if set; otherwise load from disk."""
    global _policy
    if _policy is not None:
        return _policy
    path = Path(_policy_path).expanduser()
    if path.exists():
        try:
            _policy = Policy.from_yaml(path)
            return _policy
        except Exception:
            pass
    _policy = Policy()
    return _policy


def _save_policy(policy: Policy) -> None:
    """Save policy to disk."""
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML required: pip install pyyaml") from None
    path = Path(_policy_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(policy.to_dict(), f, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _install() -> None:
    """Install Gate: write policy, hook script, and register in Claude Code settings."""
    import json as _json

    # 1. Write default policy (only if not already exists)
    policy_path = Path.home() / ".gate" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    if not policy_path.exists():
        try:
            import yaml
            policy_path.write_text(
                yaml.dump(Policy.default().to_dict(), default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
            print(f"[Gate] Wrote default policy → {policy_path}")
        except ImportError:
            # Fallback: write the examples/default.yaml content verbatim
            _default_yaml = """\
# Gate policy — rules evaluated top-to-bottom, first match wins
# decision: "block" (reject the tool call) or "warn" (log but allow)
rules:
  - name: no-rm-rf-root
    tool: bash
    match: 'rm\\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\\s+/(?!workspace)'
    decision: block
    reason: "rm -rf on paths outside /workspace is not allowed"

  - name: no-force-push-main
    tool: bash
    match: 'git push.*(--force|origin main|origin master)'
    decision: block
    reason: "Force push to main/master is not allowed"

  - name: no-drop-database
    tool: bash
    match: 'DROP\\s+DATABASE|DROP\\s+TABLE\\s+(?!IF)'
    decision: block
    reason: "DROP without IF EXISTS is not allowed — use IF EXISTS"

  - name: warn-curl-pipe
    tool: bash
    match: 'curl.*\\|.*sh|wget.*\\|.*sh'
    decision: warn
    reason: "Piping curl/wget to shell — ensure the source is trusted"
"""
            policy_path.write_text(_default_yaml, encoding="utf-8")
            print(f"[Gate] Wrote default policy → {policy_path}")
    else:
        print(f"[Gate] Policy already exists → {policy_path} (not overwritten)")

    # 2. Write PreToolUse hook script
    hooks_dir = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "gate-pre-tool-use.py"
    hook_path.write_text(_HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(0o755)
    print(f"[Gate] Wrote hook script → {hook_path}")

    # 3. Register in Claude Code settings.json
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        with open(settings_path, encoding="utf-8") as f:
            settings: dict = _json.load(f)
    else:
        settings = {}

    # Register MCP server
    mcpServers = settings.setdefault("mcpServers", {})
    mcpServers["north9-gate"] = {
        "command": "python",
        "args": ["-m", "north9.gate"],
        "env": {},
    }

    # Register PreToolUse hook
    hooks = settings.setdefault("hooks", {})
    pre_tool_use_list = hooks.setdefault("PreToolUse", [])
    hook_command = f'"python" "{hook_path}"'
    # Check if already registered
    already_registered = any(
        any(
            h.get("command") == hook_command
            for h in entry.get("hooks", [])
        )
        for entry in pre_tool_use_list
    )
    if not already_registered:
        pre_tool_use_list.append({
            "hooks": [{"type": "command", "command": hook_command, "timeout": 5}]
        })

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        _json.dump(settings, f, indent=2)
    print(f"[Gate] Registered in settings → {settings_path}")

    # 4. Add CLAUDE.md section
    claude_md_path = Path.home() / ".claude" / "CLAUDE.md"
    gate_section = """
## Gate

Policy enforcement — blocks dangerous tool calls before they execute.
```
gate_status()                                   # see active rules
gate_check("bash", '{"command": "rm -rf /"}')  # test a call
gate_add_rule("no-prod", "write_file", "/prod/", "block", "No writes to prod")
gate_remove_rule("no-prod")
```
Policy file: ~/.gate/policy.yaml — edit directly for complex rules.
"""
    if claude_md_path.exists():
        existing = claude_md_path.read_text(encoding="utf-8")
        if "## Gate" not in existing:
            claude_md_path.write_text(existing + gate_section, encoding="utf-8")
            print(f"[Gate] Added Gate section to {claude_md_path}")
        else:
            print(f"[Gate] Gate section already in {claude_md_path} (not overwritten)")
    else:
        claude_md_path.write_text(gate_section.lstrip(), encoding="utf-8")
        print(f"[Gate] Created {claude_md_path}")

    print("[Gate] Installation complete.")


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

if _MCP_AVAILABLE:
    mcp = FastMCP("gate")

    @mcp.tool()
    def gate_status() -> str:
        """Show active policy rules and policy file path."""
        policy = _load_policy()
        path = Path(_policy_path).expanduser()
        lines = [f"Policy file: {path}", f"Rules ({len(policy.rules)}):"]
        if not policy.rules:
            lines.append("  (none)")
        for rule in policy.rules:
            lines.append(
                f"  [{rule.decision.upper()}] {rule.name} — tool={rule.tool!r}  match={rule.match!r}"
            )
            lines.append(f"    reason: {rule.reason}")
        return "\n".join(lines)

    @mcp.tool()
    def gate_check(tool_name: str, tool_input_json: str) -> str:
        """Test a hypothetical tool call against the current policy.

        Returns allow/block decision.
        """
        policy = _load_policy()
        try:
            tool_input: dict[str, Any] = json.loads(tool_input_json)
        except json.JSONDecodeError as exc:
            return f"Error: invalid JSON for tool_input — {exc}"

        result = policy.evaluate(tool_name, tool_input)
        if result.blocked:
            rule_name = result.rule.name if result.rule else "unknown"
            return f"BLOCK — rule={rule_name!r}  reason={result.reason!r}"
        return f"ALLOW — no blocking rules matched for tool={tool_name!r}"

    @mcp.tool()
    def gate_add_rule(
        name: str,
        tool: str,
        match: str,
        decision: str = "block",
        reason: str = "",
    ) -> str:
        """Add a rule to the policy file.

        Args:
            name: Rule name (e.g. "no-prod-writes")
            tool: Tool name or glob pattern (e.g. "bash", "write_file", "*")
            match: Regex to match against tool input string
            decision: "block" or "warn"
            reason: Message shown when rule fires
        """
        global _policy
        policy = _load_policy()

        # Check for duplicate name
        if any(r.name == name for r in policy.rules):
            return f"Error: rule {name!r} already exists. Remove it first with gate_remove_rule."

        if decision not in ("block", "warn"):
            return f"Error: decision must be 'block' or 'warn', got {decision!r}"

        rule = Rule(
            name=name,
            tool=tool,
            match=match,
            decision=decision,  # type: ignore[arg-type]
            reason=reason or f"Policy violation: {name}",
        )
        policy.add(rule)
        _policy = policy

        try:
            _save_policy(policy)
            return f"Added rule {name!r} ({decision}) and saved to {Path(_policy_path).expanduser()}"
        except Exception as exc:
            return f"Rule added to memory but failed to save: {exc}"

    @mcp.tool()
    def gate_remove_rule(name: str) -> str:
        """Remove a rule by name."""
        global _policy
        policy = _load_policy()

        before = len(policy.rules)
        policy.rules = [r for r in policy.rules if r.name != name]
        if len(policy.rules) == before:
            return f"Error: no rule named {name!r} found."

        _policy = policy
        try:
            _save_policy(policy)
            return f"Removed rule {name!r} and saved to {Path(_policy_path).expanduser()}"
        except Exception as exc:
            return f"Rule removed from memory but failed to save: {exc}"

    @mcp.tool()
    def gate_reload() -> str:
        """Reload policy from disk (after manual edits to the YAML file)."""
        global _policy
        path = Path(_policy_path).expanduser()
        if not path.exists():
            return f"Policy file not found: {path}"
        try:
            _policy = Policy.from_yaml(path)
            return f"Reloaded {len(_policy.rules)} rules from {path}"
        except Exception as exc:
            return f"Error reloading policy: {exc}"

    def main() -> None:
        import argparse

        parser = argparse.ArgumentParser(prog="gate", description="Gate — policy enforcement MCP server")
        parser.add_argument("--install", action="store_true", help="Install Gate (write policy + hook + register)")
        args = parser.parse_args()

        if args.install:
            _install()
            return

        mcp.run()

else:
    # MCP not available — provide stub so module is importable
    def main() -> None:  # type: ignore[misc]
        import argparse

        parser = argparse.ArgumentParser(prog="gate", description="Gate — policy enforcement MCP server")
        parser.add_argument("--install", action="store_true", help="Install Gate")
        args = parser.parse_args()

        if args.install:
            _install()
            return

        print("Error: mcp package not installed. Run: pip install 'gate[mcp]'", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
