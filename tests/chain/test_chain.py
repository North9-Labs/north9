"""Tests for Chain workflow runner."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import north9.chain.mcp as chain_mcp
from north9.chain.core import (
    Step,
    StepResult,
    ToolExecutor,
    Workflow,
    WorkflowResult,
    _interpolate,
)

# ---------------------------------------------------------------------------
# Mock executor
# ---------------------------------------------------------------------------


class MockExecutor(ToolExecutor):
    def __init__(self, responses: dict[str, str] | None = None):
        super().__init__()
        self._responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool_name: str, **kwargs):
        self.calls.append((tool_name, kwargs))
        if tool_name in self._responses:
            return self._responses[tool_name]
        if tool_name.startswith("fail_"):
            raise ValueError(f"Tool {tool_name} failed")
        return f"output from {tool_name}"


# ---------------------------------------------------------------------------
# Step dataclass
# ---------------------------------------------------------------------------


def test_step_creation():
    step = Step(id="s1", tool="my_tool", args={"key": "val"})
    assert step.id == "s1"
    assert step.tool == "my_tool"
    assert step.args == {"key": "val"}
    assert step.on_error == "stop"
    assert step.description == ""


def test_step_custom_on_error():
    step = Step(id="s1", tool="t", args={}, on_error="continue", description="desc")
    assert step.on_error == "continue"
    assert step.description == "desc"


# ---------------------------------------------------------------------------
# _interpolate
# ---------------------------------------------------------------------------


def _make_result(step_id: str, output: str) -> StepResult:
    step = Step(id=step_id, tool="dummy", args={})
    return StepResult(step=step, output=output, success=True)


def test_interpolate_replaces_output():
    ctx = {"fetch": _make_result("fetch", "hello world")}
    result = _interpolate("Got: {{ fetch.output }}", ctx)
    assert result == "Got: hello world"


def test_interpolate_unchanged_if_missing():
    ctx: dict = {}
    result = _interpolate("{{ missing.output }}", ctx)
    assert result == "{{ missing.output }}"


def test_interpolate_nested_dict():
    ctx = {"step1": _make_result("step1", "nested value")}
    data = {"outer": {"inner": "{{ step1.output }}"}}
    result = _interpolate(data, ctx)
    assert result == {"outer": {"inner": "nested value"}}


def test_interpolate_list():
    ctx = {"a": _make_result("a", "alpha")}
    result = _interpolate(["x", "{{ a.output }}", "z"], ctx)
    assert result == ["x", "alpha", "z"]


def test_interpolate_non_string_passthrough():
    ctx: dict = {}
    assert _interpolate(42, ctx) == 42
    assert _interpolate(None, ctx) is None


# ---------------------------------------------------------------------------
# Workflow.from_dict
# ---------------------------------------------------------------------------


def test_from_dict_basic():
    data = {
        "name": "test-workflow",
        "steps": [
            {"id": "s1", "tool": "tool_a", "args": {"x": 1}},
            {"id": "s2", "tool": "tool_b", "args": {}, "on_error": "continue"},
        ],
    }
    wf = Workflow.from_dict(data)
    assert wf.name == "test-workflow"
    assert len(wf.steps) == 2
    assert wf.steps[0].id == "s1"
    assert wf.steps[0].tool == "tool_a"
    assert wf.steps[1].on_error == "continue"


def test_from_dict_defaults():
    data = {"steps": [{"tool": "some_tool", "args": {}}]}
    wf = Workflow.from_dict(data)
    assert wf.name == "workflow"
    assert wf.steps[0].id == "step_0"


# ---------------------------------------------------------------------------
# Workflow.run
# ---------------------------------------------------------------------------


def test_run_calls_tools_in_order():
    wf = Workflow.from_dict({
        "name": "order-test",
        "steps": [
            {"id": "a", "tool": "tool_a", "args": {}},
            {"id": "b", "tool": "tool_b", "args": {}},
        ],
    })
    exe = MockExecutor()
    result = wf.run(exe)
    assert result.completed
    assert [c[0] for c in exe.calls] == ["tool_a", "tool_b"]


def test_run_passes_output_between_steps():
    wf = Workflow.from_dict({
        "name": "pass-test",
        "steps": [
            {"id": "fetch", "tool": "fetcher", "args": {}},
            {"id": "store", "tool": "storer", "args": {"content": "{{ fetch.output }}"}},
        ],
    })
    exe = MockExecutor({"fetcher": "fetched data"})
    result = wf.run(exe)
    assert result.completed
    # Second call should have received the interpolated value
    assert exe.calls[1] == ("storer", {"content": "fetched data"})


def test_run_stops_on_error_by_default():
    wf = Workflow.from_dict({
        "name": "stop-test",
        "steps": [
            {"id": "ok", "tool": "good_tool", "args": {}},
            {"id": "bad", "tool": "fail_step", "args": {}},
            {"id": "never", "tool": "should_not_run", "args": {}},
        ],
    })
    exe = MockExecutor()
    result = wf.run(exe)
    assert not result.completed
    assert len(result.results) == 2
    assert result.results[1].success is False
    # Third step never ran
    assert not any(r.step.id == "never" for r in result.results)


def test_run_continues_on_error():
    wf = Workflow.from_dict({
        "name": "continue-test",
        "steps": [
            {"id": "bad", "tool": "fail_step", "args": {}, "on_error": "continue"},
            {"id": "good", "tool": "good_tool", "args": {}},
        ],
    })
    exe = MockExecutor()
    result = wf.run(exe)
    assert result.completed
    assert len(result.results) == 2
    assert result.results[0].success is False
    assert result.results[1].success is True


def test_run_skip_on_error():
    wf = Workflow.from_dict({
        "name": "skip-test",
        "steps": [
            {"id": "bad", "tool": "fail_step", "args": {}, "on_error": "skip"},
            {"id": "good", "tool": "good_tool", "args": {}},
        ],
    })
    exe = MockExecutor()
    result = wf.run(exe)
    assert result.completed
    assert len(result.results) == 2


# ---------------------------------------------------------------------------
# WorkflowResult
# ---------------------------------------------------------------------------


def _build_result(name: str = "test") -> WorkflowResult:
    step_ok = Step(id="ok", tool="tool_ok", args={})
    step_fail = Step(id="fail", tool="tool_fail", args={})
    return WorkflowResult(
        name=name,
        results=[
            StepResult(step=step_ok, output="good output", success=True),
            StepResult(step=step_fail, output="", success=False, error="boom"),
        ],
        completed=False,
    )


def test_workflow_result_succeeded_failed():
    r = _build_result()
    assert len(r.succeeded) == 1
    assert len(r.failed) == 1


def test_workflow_result_format_report():
    r = _build_result("my-flow")
    report = r.format_report()
    assert "my-flow" in report
    assert "✓" in report
    assert "✗" in report
    assert "boom" in report
    assert "1/2 steps succeeded" in report


def test_workflow_result_to_dict():
    r = _build_result("dict-flow")
    d = r.to_dict()
    assert d["name"] == "dict-flow"
    assert d["succeeded"] == 1
    assert d["failed"] == 1
    assert "results" in d
    assert len(d["results"]) == 2
    assert "id" in d["results"][0]
    assert "tool" in d["results"][0]
    assert "success" in d["results"][0]


# ---------------------------------------------------------------------------
# Workflow.from_yaml
# ---------------------------------------------------------------------------


def test_from_yaml(tmp_path: Path):
    workflow_data = {
        "name": "yaml-test",
        "steps": [
            {"id": "step1", "tool": "tool_x", "args": {"key": "value"}},
        ],
    }
    yaml_file = tmp_path / "test_workflow.yaml"
    yaml_file.write_text(yaml.dump(workflow_data), encoding="utf-8")

    wf = Workflow.from_yaml(yaml_file)
    assert wf.name == "yaml-test"
    assert len(wf.steps) == 1
    assert wf.steps[0].id == "step1"
    assert wf.steps[0].tool == "tool_x"
    assert wf.steps[0].args == {"key": "value"}


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_executor():
    """Reset the global executor before each test."""
    original = chain_mcp._executor
    chain_mcp._executor = MockExecutor()
    yield
    chain_mcp._executor = original


def test_chain_validate_valid(tmp_path: Path):
    workflow_data = {
        "name": "valid-wf",
        "steps": [{"id": "s1", "tool": "some_tool", "args": {}}],
    }
    p = tmp_path / "valid.yaml"
    p.write_text(yaml.dump(workflow_data), encoding="utf-8")

    result = chain_mcp.chain_validate(str(p))
    assert "valid" in result.lower()
    assert "valid-wf" in result


def test_chain_validate_invalid_path():
    result = chain_mcp.chain_validate("/nonexistent/path/workflow.yaml")
    assert "error" in result.lower() or "not found" in result.lower()


def test_chain_example_research():
    result = chain_mcp.chain_example("research")
    assert "research-and-store" in result
    assert "scout_fetch" in result
    assert "index_add" in result


def test_chain_example_eval():
    result = chain_mcp.chain_example("eval")
    assert "eval-and-report" in result
    assert "forge_run" in result


def test_chain_example_unknown():
    result = chain_mcp.chain_example("unknown-template")
    assert "unknown" in result.lower()


def test_chain_run_dict_valid():
    chain_mcp._executor = MockExecutor({"my_tool": "tool result"})
    payload = json.dumps({
        "name": "inline-test",
        "steps": [{"id": "s1", "tool": "my_tool", "args": {}}],
    })
    result = chain_mcp.chain_run_dict(payload)
    assert "inline-test" in result
    assert "my_tool" in result


def test_chain_run_dict_invalid_json():
    result = chain_mcp.chain_run_dict("{not valid json}")
    assert "error" in result.lower()


def test_chain_run_dict_unregistered_tool():
    chain_mcp._executor = MockExecutor()
    payload = json.dumps({
        "name": "missing-tool-test",
        "steps": [{"id": "s1", "tool": "nonexistent_tool", "args": {}}],
    })
    result = chain_mcp.chain_run_dict(payload)
    # Should show failure but not crash
    assert "✗" in result or "nonexistent_tool" in result
