"""Tests for Grid parallel agent execution framework."""
from __future__ import annotations

import json

from north9.grid.core import Grid, GridResult, Task, TaskResult, TaskStatus, make_tasks

# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

class MockContent:
    def __init__(self, text: str) -> None:
        self.text = text


class MockUsage:
    input_tokens = 5
    output_tokens = 10


class MockMessages:
    def create(self, **kwargs):
        class R:
            content = [MockContent(f"response to: {kwargs['messages'][0]['content'][:20]}")]
            usage = MockUsage()

        return R()


class MockClient:
    messages = MockMessages()


class FailingMessages:
    """Mock that raises for the first prompt containing 'FAIL'."""

    def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        if "FAIL" in prompt:
            raise ValueError("Simulated API error")

        class R:
            content = [MockContent(f"response to: {prompt[:20]}")]
            usage = MockUsage()

        return R()


class FailingClient:
    messages = FailingMessages()


# ---------------------------------------------------------------------------
# Core tests
# ---------------------------------------------------------------------------

def test_grid_map_returns_three_results():
    grid = Grid(client=MockClient())
    result = grid.map(["prompt one", "prompt two", "prompt three"])
    assert isinstance(result, GridResult)
    assert len(result.results) == 3


def test_grid_map_all_done():
    grid = Grid(client=MockClient())
    result = grid.map(["a", "b", "c"])
    for r in result.results:
        assert r.status == TaskStatus.DONE


def test_grid_result_succeeded_length():
    grid = Grid(client=MockClient())
    result = grid.map(["x", "y", "z"])
    assert len(result.succeeded) == 3


def test_grid_result_outputs_three_strings():
    grid = Grid(client=MockClient())
    result = grid.map(["p1", "p2", "p3"])
    outputs = result.outputs()
    assert len(outputs) == 3
    for o in outputs:
        assert isinstance(o, str)


def test_grid_result_timing_both_positive():
    grid = Grid(client=MockClient())
    result = grid.map(["a", "b", "c"])
    assert result.wall_time_ms > 0
    assert result.total_latency_ms > 0


def test_grid_result_format_summary_contains_task_count():
    grid = Grid(client=MockClient())
    result = grid.map(["a", "b", "c"])
    summary = result.format_summary()
    assert "3 tasks" in summary


def test_grid_result_to_dict_keys():
    grid = Grid(client=MockClient())
    result = grid.map(["a", "b"])
    d = result.to_dict()
    assert "results" in d
    assert "speedup" in d
    assert "total" in d
    assert "wall_time_ms" in d
    assert "total_latency_ms" in d


def test_make_tasks_creates_correct_objects():
    tasks = make_tasks(["hello", "world"], model="claude-opus-4-7", system="sys")
    assert len(tasks) == 2
    for t in tasks:
        assert isinstance(t, Task)
        assert t.model == "claude-opus-4-7"
        assert t.system == "sys"
        assert len(t.id) == 8


def test_grid_run_on_result_callback():
    grid = Grid(client=MockClient())
    tasks = make_tasks(["a", "b", "c"])
    called: list[TaskResult] = []
    grid.run(tasks, on_result=lambda r: called.append(r))
    assert len(called) == 3


def test_grid_run_failed_task():
    grid = Grid(client=FailingClient())
    tasks = make_tasks(["normal prompt", "FAIL this one", "another normal"])
    result = grid.run(tasks)
    assert len(result.failed) == 1
    assert result.failed[0].status == TaskStatus.FAILED
    assert result.failed[0].error is not None
    assert len(result.succeeded) == 2


def test_grid_result_order_preserved():
    """Results should be sorted back to original task order."""
    grid = Grid(client=MockClient())
    prompts = [f"prompt {i}" for i in range(5)]
    result = grid.map(prompts)
    # Each output should reference the corresponding prompt prefix
    for i, r in enumerate(result.results):
        assert r.task.prompt == prompts[i]


def test_task_result_to_dict():
    task = Task(id="abc12345", prompt="test", metadata={"foo": "bar"})
    tr = TaskResult(
        task=task,
        status=TaskStatus.DONE,
        output="hello",
        tokens_used=15,
        latency_ms=42.5,
    )
    d = tr.to_dict()
    assert d["id"] == "abc12345"
    assert d["status"] == "done"
    assert d["output"] == "hello"
    assert d["tokens_used"] == 15
    assert d["metadata"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------

def _setup_mcp_client():
    import grid.mcp as m
    m._client = MockClient()
    return m


def test_mcp_grid_map_valid_json():
    m = _setup_mcp_client()
    result = m.grid_map(
        prompts_json='["Analyze file A", "Analyze file B"]',
        model="claude-haiku-4-5-20251001",
    )
    # Should be valid JSON
    data = json.loads(result)
    assert "results" in data
    assert data["total"] == 2


def test_mcp_grid_map_invalid_json():
    m = _setup_mcp_client()
    result = m.grid_map(prompts_json="not valid json")
    assert result.startswith("Error:")


def test_mcp_grid_map_non_array_json():
    m = _setup_mcp_client()
    result = m.grid_map(prompts_json='{"key": "value"}')
    assert result.startswith("Error:")


def test_mcp_grid_run_valid():
    m = _setup_mcp_client()
    tasks_json = json.dumps([
        {"prompt": "hello", "model": "claude-haiku-4-5-20251001"},
        {"prompt": "world", "model": "claude-haiku-4-5-20251001"},
    ])
    result = m.grid_run(tasks_json=tasks_json)
    data = json.loads(result)
    assert data["total"] == 2


def test_mcp_grid_run_missing_prompt():
    m = _setup_mcp_client()
    tasks_json = json.dumps([{"model": "claude-haiku-4-5-20251001"}])
    result = m.grid_run(tasks_json=tasks_json)
    assert result.startswith("Error:")


def test_mcp_grid_status_returns_string():
    m = _setup_mcp_client()
    result = m.grid_status()
    assert isinstance(result, str)
    data = json.loads(result)
    assert "api_key" in data
    assert "default_model" in data


def test_mcp_grid_map_no_api_key(monkeypatch):
    import grid.mcp as m
    m._client = None
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = m.grid_map(prompts_json='["test"]')
    assert result.startswith("Error: ANTHROPIC_API_KEY not set")


def test_mcp_grid_run_no_api_key(monkeypatch):
    import grid.mcp as m
    m._client = None
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = m.grid_run(tasks_json='[{"prompt": "test"}]')
    assert result.startswith("Error: ANTHROPIC_API_KEY not set")
