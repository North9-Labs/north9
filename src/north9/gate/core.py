"""Gate — policy enforcement for AI agent tool calls."""
from __future__ import annotations

import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

Decision = Literal["allow", "block", "warn"]
_MAX_REGEX_LEN = 500
_REGEX_TIMEOUT_SEC = 0.5


def _safe_regex_search(pattern: str, text: str) -> bool:
    """Run re.search with length cap and SIGALRM timeout to prevent ReDoS."""
    if not pattern or len(pattern) > _MAX_REGEX_LEN:
        return False
    try:
        compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        return False

    if hasattr(signal, "SIGALRM"):
        def _alarm(signum: int, frame: Any) -> None:
            raise TimeoutError()
        old = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, _REGEX_TIMEOUT_SEC)
        try:
            return bool(compiled.search(text))
        except TimeoutError:
            return False
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
    else:
        return bool(compiled.search(text))


@dataclass
class Rule:
    name: str
    tool: str          # tool name pattern, "*" matches all, supports glob
    match: str         # regex pattern to match against the serialized tool_input
    decision: Decision  # "block" or "warn"
    reason: str        # message shown when rule fires

    def matches_tool(self, tool_name: str) -> bool:
        if self.tool == "*":
            return True
        # simple glob: "bash*" matches "bash", "bash_run" etc
        import fnmatch
        return fnmatch.fnmatch(tool_name, self.tool)

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
        """Returns (matched, reason). matched=True means rule fired."""
        if not self.matches_tool(tool_name):
            return False, ""
        input_str = str(tool_input)
        if _safe_regex_search(self.match, input_str):
            return True, self.reason
        return False, ""

    @classmethod
    def from_dict(cls, d: dict) -> Rule:
        return cls(
            name=d.get("name", "unnamed"),
            tool=d.get("tool", "*"),
            match=d.get("match", ""),
            decision=d.get("decision", "block"),
            reason=d.get("reason", "Policy violation"),
        )


@dataclass
class PolicyResult:
    allowed: bool
    rule: Rule | None = None
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed


class Policy:
    """Collection of rules evaluated against tool calls."""

    def __init__(self, rules: list[Rule] | None = None):
        self.rules: list[Rule] = rules or []

    def add(self, rule: Rule) -> None:
        self.rules.append(rule)

    def evaluate(self, tool_name: str, tool_input: dict[str, Any]) -> PolicyResult:
        for rule in self.rules:
            matched, reason = rule.check(tool_name, tool_input)
            if matched:
                if rule.decision == "block":
                    return PolicyResult(allowed=False, rule=rule, reason=reason)
                # "warn" — log but allow (rule is recorded for callers to inspect)
        return PolicyResult(allowed=True)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Policy:
        if not _YAML_AVAILABLE:
            raise ImportError("PyYAML required: pip install pyyaml")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        rules = [Rule.from_dict(r) for r in data.get("rules", [])]
        return cls(rules=rules)

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        rules = [Rule.from_dict(r) for r in data.get("rules", [])]
        return cls(rules=rules)

    @classmethod
    def default(cls) -> Policy:
        """Sensible default policy for most use cases."""
        return cls.from_dict({
            "rules": [
                {
                    "name": "no-rm-rf-root",
                    "tool": "bash",
                    "match": r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/(?!workspace)",
                    "decision": "block",
                    "reason": "Blocked: rm -rf on paths outside /workspace is not allowed",
                },
                {
                    "name": "no-force-push-main",
                    "tool": "bash",
                    "match": r"git push.*--force.*(?:main|master)|git push.*(?:main|master).*--force",
                    "decision": "block",
                    "reason": "Blocked: force push to main/master is not allowed",
                },
                {
                    "name": "no-drop-database",
                    "tool": "bash",
                    "match": r"DROP\s+DATABASE|DROP\s+TABLE\s+(?!IF)",
                    "decision": "block",
                    "reason": "Blocked: DROP DATABASE and DROP TABLE without IF EXISTS are not allowed",
                },
                {
                    "name": "no-curl-pipe-sh",
                    "tool": "bash",
                    "match": r"curl.*\|.*sh|wget.*\|.*sh",
                    "decision": "warn",
                    "reason": "Warning: piping curl/wget to shell — ensure the source is trusted",
                },
            ]
        })

    def to_dict(self) -> dict:
        return {
            "rules": [
                {
                    "name": r.name,
                    "tool": r.tool,
                    "match": r.match,
                    "decision": r.decision,
                    "reason": r.reason,
                }
                for r in self.rules
            ]
        }
