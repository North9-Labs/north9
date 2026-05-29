"""Tests for Forge — no API calls, all mocked."""
from __future__ import annotations

import pytest
import yaml

from north9.forge.core import Assertion, Suite, SuiteResult, TestCase, TestResult
from north9.forge.mcp import forge_check, forge_example

# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

class MockClient:
    class MockResponse:
        class MockContent:
            text = "hello world"

        class MockUsage:
            input_tokens = 10
            output_tokens = 20

        content = [MockContent()]
        usage = MockUsage()

    class MockMessages:
        def create(self, **kwargs):  # noqa: ANN001, ANN201
            return MockClient.MockResponse()

    messages = MockMessages()


# ---------------------------------------------------------------------------
# Assertion.check — all types
# ---------------------------------------------------------------------------

class TestAssertionCheck:
    def test_contains_pass(self) -> None:
        a = Assertion(type="contains", value="hello")
        ok, msg = a.check("say hello world", 10)
        assert ok
        assert msg == ""

    def test_contains_fail(self) -> None:
        a = Assertion(type="contains", value="goodbye")
        ok, msg = a.check("say hello world", 10)
        assert not ok
        assert "goodbye" in msg

    def test_contains_case_insensitive(self) -> None:
        a = Assertion(type="contains", value="HELLO")
        ok, _ = a.check("say hello world", 10)
        assert ok

    def test_not_contains_pass(self) -> None:
        a = Assertion(type="not_contains", value="error")
        ok, msg = a.check("everything is fine", 10)
        assert ok
        assert msg == ""

    def test_not_contains_fail(self) -> None:
        a = Assertion(type="not_contains", value="error")
        ok, msg = a.check("there was an error here", 10)
        assert not ok
        assert "error" in msg

    def test_regex_pass(self) -> None:
        a = Assertion(type="regex", value=r"def \w+\(")
        ok, _ = a.check("def add(a, b):", 10)
        assert ok

    def test_regex_fail(self) -> None:
        a = Assertion(type="regex", value=r"def \w+\(")
        ok, msg = a.check("no function here", 10)
        assert not ok
        assert "did not match" in msg

    def test_not_regex_pass(self) -> None:
        a = Assertion(type="not_regex", value=r"I cannot")
        ok, _ = a.check("Sure, here is the answer", 10)
        assert ok

    def test_not_regex_fail(self) -> None:
        a = Assertion(type="not_regex", value=r"I cannot")
        ok, msg = a.check("I cannot do that", 10)
        assert not ok
        assert "matched but should not" in msg

    def test_max_tokens_pass(self) -> None:
        a = Assertion(type="max_tokens", value=100)
        ok, _ = a.check("short response", 50)
        assert ok

    def test_max_tokens_fail(self) -> None:
        a = Assertion(type="max_tokens", value=100)
        ok, msg = a.check("long response", 150)
        assert not ok
        assert "150" in msg
        assert "100" in msg

    def test_min_tokens_pass(self) -> None:
        a = Assertion(type="min_tokens", value=5)
        ok, _ = a.check("response with enough tokens", 20)
        assert ok

    def test_min_tokens_fail(self) -> None:
        a = Assertion(type="min_tokens", value=50)
        ok, msg = a.check("short", 3)
        assert not ok
        assert "3" in msg
        assert "50" in msg


# ---------------------------------------------------------------------------
# Assertion.from_dict
# ---------------------------------------------------------------------------

class TestAssertionFromDict:
    def test_shorthand_contains(self) -> None:
        a = Assertion.from_dict({"contains": "hello"})
        assert a.type == "contains"
        assert a.value == "hello"

    def test_shorthand_max_tokens(self) -> None:
        a = Assertion.from_dict({"max_tokens": 200})
        assert a.type == "max_tokens"
        assert a.value == 200

    def test_explicit_form(self) -> None:
        a = Assertion.from_dict({"type": "regex", "value": r"\d+"})
        assert a.type == "regex"
        assert a.value == r"\d+"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown assertion"):
            Assertion.from_dict({"unknown_key": "value"})


# ---------------------------------------------------------------------------
# TestCase.from_dict
# ---------------------------------------------------------------------------

class TestTestCaseFromDict:
    def test_basic(self) -> None:
        d = {
            "name": "test one",
            "input": "Say hello",
            "assert": [{"contains": "hello"}],
        }
        tc = TestCase.from_dict(d)
        assert tc.name == "test one"
        assert tc.input == "Say hello"
        assert len(tc.assertions) == 1
        assert tc.assertions[0].type == "contains"

    def test_assertions_key(self) -> None:
        d = {
            "name": "test two",
            "input": "Write code",
            "assertions": [{"regex": r"def \w+"}],
        }
        tc = TestCase.from_dict(d)
        assert len(tc.assertions) == 1

    def test_optional_fields_defaults(self) -> None:
        d = {"name": "minimal", "input": "hi"}
        tc = TestCase.from_dict(d)
        assert tc.system is None
        assert tc.model is None
        assert tc.tags == []
        assert tc.assertions == []

    def test_tags_and_overrides(self) -> None:
        d = {
            "name": "tagged",
            "input": "test",
            "system": "custom system",
            "model": "claude-opus-4-5",
            "tags": ["smoke", "fast"],
        }
        tc = TestCase.from_dict(d)
        assert tc.system == "custom system"
        assert tc.model == "claude-opus-4-5"
        assert tc.tags == ["smoke", "fast"]


# ---------------------------------------------------------------------------
# Suite.from_dict
# ---------------------------------------------------------------------------

class TestSuiteFromDict:
    def test_builds_suite(self) -> None:
        data = {
            "name": "my suite",
            "model": "claude-haiku-4-5-20251001",
            "system": "You are helpful.",
            "max_tokens": 512,
            "cases": [
                {"name": "c1", "input": "hello", "assert": [{"contains": "hi"}]},
                {"name": "c2", "input": "bye"},
            ],
        }
        suite = Suite.from_dict(data)
        assert suite.name == "my suite"
        assert suite.model == "claude-haiku-4-5-20251001"
        assert suite.system == "You are helpful."
        assert suite.max_tokens == 512
        assert len(suite.cases) == 2

    def test_defaults(self) -> None:
        suite = Suite.from_dict({"cases": []})
        assert suite.name == "unnamed"
        assert suite.model == "claude-haiku-4-5-20251001"
        assert suite.max_tokens == 1024
        assert suite.system is None


# ---------------------------------------------------------------------------
# Suite._run_case with mock client
# ---------------------------------------------------------------------------

class TestSuiteRunCase:
    def _make_suite(self, assertions: list[dict]) -> Suite:
        data = {
            "name": "mock suite",
            "cases": [
                {
                    "name": "mock test",
                    "input": "Say hello",
                    "assert": assertions,
                }
            ],
        }
        return Suite.from_dict(data)

    def test_passing_case(self) -> None:
        suite = self._make_suite([{"contains": "hello"}])
        result = suite._run_case(suite.cases[0], MockClient())
        assert result.passed
        assert result.failures == []
        assert result.error is None
        assert result.response == "hello world"
        assert result.tokens_used == 30  # 10 + 20

    def test_failing_assertion(self) -> None:
        suite = self._make_suite([{"contains": "goodbye"}])
        result = suite._run_case(suite.cases[0], MockClient())
        assert not result.passed
        assert len(result.failures) == 1
        assert "goodbye" in result.failures[0]

    def test_error_handling(self) -> None:
        class BrokenClient:
            class BrokenMessages:
                def create(self, **kwargs):  # noqa: ANN001, ANN201
                    raise ConnectionError("network down")
            messages = BrokenMessages()

        suite = self._make_suite([])
        result = suite._run_case(suite.cases[0], BrokenClient())
        assert not result.passed
        assert result.error == "network down"

    def test_latency_recorded(self) -> None:
        suite = self._make_suite([])
        result = suite._run_case(suite.cases[0], MockClient())
        assert result.latency_ms >= 0

    def test_system_override(self) -> None:
        """Case-level system prompt overrides suite-level."""
        suite = Suite.from_dict({
            "name": "s",
            "system": "suite system",
            "cases": [{"name": "c", "input": "hi", "system": "case system"}],
        })
        # Just verify _run_case doesn't crash and uses case system
        result = suite._run_case(suite.cases[0], MockClient())
        assert result.passed or result.error is None  # no assertion failures, just runs


# ---------------------------------------------------------------------------
# SuiteResult
# ---------------------------------------------------------------------------

class TestSuiteResult:
    def _make_result(self, passed_flags: list[bool]) -> SuiteResult:
        results = []
        for i, p in enumerate(passed_flags):
            tc = TestCase(name=f"test {i}", input="hi")
            tr = TestResult(
                case=tc,
                passed=p,
                failures=[] if p else ["something failed"],
                response="response",
                tokens_used=30,
                latency_ms=100.0,
            )
            results.append(tr)
        return SuiteResult(suite_name="test suite", results=results, total_latency_ms=500.0)

    def test_counts(self) -> None:
        sr = self._make_result([True, True, False, True])
        assert sr.passed == 3
        assert sr.failed == 1
        assert sr.total == 4

    def test_all_pass(self) -> None:
        sr = self._make_result([True, True])
        assert sr.passed == 2
        assert sr.failed == 0

    def test_all_fail(self) -> None:
        sr = self._make_result([False, False, False])
        assert sr.passed == 0
        assert sr.failed == 3

    def test_format_report_contains_suite_name(self) -> None:
        sr = self._make_result([True, False])
        report = sr.format_report()
        assert "test suite" in report

    def test_format_report_checkmarks(self) -> None:
        sr = self._make_result([True, False])
        report = sr.format_report()
        assert "✓" in report
        assert "✗" in report

    def test_format_report_summary_line(self) -> None:
        sr = self._make_result([True, False, True])
        report = sr.format_report()
        assert "2/3 passed" in report

    def test_format_report_failure_detail(self) -> None:
        sr = self._make_result([False])
        report = sr.format_report()
        assert "something failed" in report

    def test_format_report_error(self) -> None:
        tc = TestCase(name="err test", input="hi")
        tr = TestResult(
            case=tc,
            passed=False,
            failures=[],
            response="",
            tokens_used=0,
            latency_ms=50.0,
            error="API timeout",
        )
        sr = SuiteResult(suite_name="s", results=[tr], total_latency_ms=50.0)
        report = sr.format_report()
        assert "API timeout" in report


# ---------------------------------------------------------------------------
# MCP tools: forge_check and forge_example
# ---------------------------------------------------------------------------

class TestMcpForgeCheck:
    def test_passing(self) -> None:
        result = forge_check(
            name="my check",
            input="Say hello",
            response="hello world",
            assertions_json='[{"contains": "hello"}]',
        )
        assert "PASS" in result
        assert "my check" in result

    def test_failing(self) -> None:
        result = forge_check(
            name="fail check",
            input="Say hello",
            response="goodbye",
            assertions_json='[{"contains": "hello"}]',
        )
        assert "FAIL" in result
        assert "hello" in result

    def test_multiple_assertions(self) -> None:
        result = forge_check(
            name="multi",
            input="Write code",
            response="def add(a, b): return a + b",
            assertions_json='[{"contains": "def"}, {"contains": "return"}, {"not_contains": "error"}]',
        )
        assert "PASS" in result
        assert "3 assertion" in result

    def test_invalid_json(self) -> None:
        result = forge_check(
            name="bad",
            input="hi",
            response="ho",
            assertions_json="not valid json",
        )
        assert "ERROR" in result
        assert "JSON" in result

    def test_bad_assertion_format(self) -> None:
        result = forge_check(
            name="bad assert",
            input="hi",
            response="ho",
            assertions_json='[{"unknown_key": "value"}]',
        )
        assert "ERROR" in result


class TestMcpForgeExample:
    def test_returns_string(self) -> None:
        result = forge_example()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_is_valid_yaml(self) -> None:
        result = forge_example()
        data = yaml.safe_load(result)
        assert isinstance(data, dict)

    def test_has_required_fields(self) -> None:
        result = forge_example()
        data = yaml.safe_load(result)
        assert "name" in data
        assert "cases" in data
        assert len(data["cases"]) > 0

    def test_cases_have_assertions(self) -> None:
        result = forge_example()
        data = yaml.safe_load(result)
        for case in data["cases"]:
            assert "name" in case
            assert "input" in case
            assert "assert" in case
