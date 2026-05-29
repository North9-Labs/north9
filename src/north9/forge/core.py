"""Forge — eval framework for AI agents. Define tests, run against a model, score results."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


AssertionType = Literal["contains", "not_contains", "regex", "not_regex", "max_tokens", "min_tokens"]


@dataclass
class Assertion:
    type: AssertionType
    value: str | int

    def check(self, response: str, token_count: int) -> tuple[bool, str]:
        """Returns (passed, failure_message)."""
        if self.type == "contains":
            ok = str(self.value).lower() in response.lower()
            return ok, f"expected to contain {self.value!r}" if not ok else ""
        elif self.type == "not_contains":
            ok = str(self.value).lower() not in response.lower()
            return ok, f"expected NOT to contain {self.value!r}" if not ok else ""
        elif self.type == "regex":
            ok = bool(re.search(str(self.value), response, re.IGNORECASE))
            return ok, f"regex {self.value!r} did not match" if not ok else ""
        elif self.type == "not_regex":
            ok = not bool(re.search(str(self.value), response, re.IGNORECASE))
            return ok, f"regex {self.value!r} matched but should not" if not ok else ""
        elif self.type == "max_tokens":
            ok = token_count <= int(self.value)
            return ok, f"response used {token_count} tokens (max {self.value})" if not ok else ""
        elif self.type == "min_tokens":
            ok = token_count >= int(self.value)
            return ok, f"response used {token_count} tokens (min {self.value})" if not ok else ""
        return True, ""

    @classmethod
    def from_dict(cls, d: dict) -> Assertion:
        # Support {"contains": "value"} shorthand or {"type": "contains", "value": "..."}
        if "type" in d and "value" in d:
            return cls(type=d["type"], value=d["value"])
        for k in ("contains", "not_contains", "regex", "not_regex", "max_tokens", "min_tokens"):
            if k in d:
                return cls(type=k, value=d[k])
        raise ValueError(f"Unknown assertion format: {d}")


@dataclass
class EvalCase:
    name: str
    input: str
    assertions: list[Assertion] = field(default_factory=list)
    system: str | None = None  # overrides suite-level system
    model: str | None = None   # overrides suite-level model
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> EvalCase:
        assert_raw = d.get("assert", d.get("assertions", []))
        assertions = [Assertion.from_dict(a) for a in assert_raw]
        return cls(
            name=d["name"],
            input=d["input"],
            assertions=assertions,
            system=d.get("system"),
            model=d.get("model"),
            tags=d.get("tags", []),
        )


@dataclass
class EvalResult:
    case: EvalCase
    passed: bool
    failures: list[str]
    response: str
    tokens_used: int
    latency_ms: float
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.case.name,
            "passed": self.passed,
            "failures": self.failures,
            "tokens_used": self.tokens_used,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


@dataclass
class SuiteResult:
    suite_name: str
    results: list[EvalResult]
    total_latency_ms: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    def format_report(self) -> str:
        lines = [f"Forge — {self.suite_name}", "=" * 50]
        for r in self.results:
            status = "✓" if r.passed else "✗"
            line = f"  {status} {r.case.name}"
            if r.error:
                line += f" [ERROR: {r.error}]"
            lines.append(line)
            for f in r.failures:
                lines.append(f"      → {f}")
        lines.append("")
        lines.append(f"  {self.passed}/{self.total} passed  ({round(self.total_latency_ms/1000, 1)}s)")
        return "\n".join(lines)


class Suite:
    """A collection of test cases to run against a model."""

    def __init__(
        self,
        name: str,
        cases: list[EvalCase],
        model: str = "claude-haiku-4-5-20251001",
        system: str | None = None,
        max_tokens: int = 1024,
    ):
        self.name = name
        self.cases = cases
        self.model = model
        self.system = system
        self.max_tokens = max_tokens

    @classmethod
    def from_yaml(cls, path: str | Path) -> Suite:
        if not _YAML_AVAILABLE:
            raise ImportError("PyYAML is required: pip install pyyaml")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        cases = [EvalCase.from_dict(c) for c in data.get("cases", [])]
        return cls(
            name=data.get("name", Path(path).stem),
            cases=cases,
            model=data.get("model", "claude-haiku-4-5-20251001"),
            system=data.get("system"),
            max_tokens=data.get("max_tokens", 1024),
        )

    @classmethod
    def from_dict(cls, data: dict) -> Suite:
        cases = [EvalCase.from_dict(c) for c in data.get("cases", [])]
        return cls(
            name=data.get("name", "unnamed"),
            cases=cases,
            model=data.get("model", "claude-haiku-4-5-20251001"),
            system=data.get("system"),
            max_tokens=data.get("max_tokens", 1024),
        )

    def run(self, client: Any) -> SuiteResult:
        """Run all test cases. client must have .messages.create() (Anthropic SDK)."""
        results = []
        suite_start = time.perf_counter()

        for case in self.cases:
            result = self._run_case(case, client)
            results.append(result)

        total_ms = (time.perf_counter() - suite_start) * 1000
        return SuiteResult(suite_name=self.name, results=results, total_latency_ms=total_ms)

    def _run_case(self, case: EvalCase, client: Any) -> EvalResult:
        model = case.model or self.model
        system = case.system or self.system
        start = time.perf_counter()
        error = None
        response_text = ""
        tokens_used = 0

        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": case.input}],
            }
            if system:
                kwargs["system"] = system

            response = client.messages.create(**kwargs)
            response_text = response.content[0].text if response.content else ""
            tokens_used = response.usage.input_tokens + response.usage.output_tokens
        except Exception as e:
            error = str(e)

        latency_ms = (time.perf_counter() - start) * 1000
        failures = []
        if not error:
            for assertion in case.assertions:
                ok, msg = assertion.check(response_text, tokens_used)
                if not ok:
                    failures.append(msg)

        return EvalResult(
            case=case,
            passed=not error and not failures,
            failures=failures,
            response=response_text,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            error=error,
        )
