"""Tests for north9.autopsy — behavioral analysis engine."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from north9.autopsy.core import (
    AutopsyReport,
    Finding,
    _estimate_cost,
    _jaccard,
    analyze_session,
)
from north9.prism.session import Frame, Session


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_tool_frame(id: int, tool: str, input_data: dict, output: str, elapsed_ms: int = 100) -> Frame:
    return Frame(
        id=id,
        type="tool",
        ts=1700000000.0 + id,
        elapsed_ms=elapsed_ms,
        input=input_data,
        output={"content": output},
        tool=tool,
    )


def _make_llm_frame(id: int, output_tokens: int, input_tokens: int = 500, model: str = "claude-sonnet-4-6") -> Frame:
    return Frame(
        id=id,
        type="llm",
        ts=1700000000.0 + id,
        elapsed_ms=300,
        input={"model": model, "messages": [{"role": "user", "content": "do the thing"}]},
        output={"usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}},
    )


def _session_file(frames: list[Frame]) -> Path:
    s = Session(frames=frames, session_id="test-session-001")
    tmp = tempfile.NamedTemporaryFile(suffix=".prism", delete=False)
    s.save(tmp.name)
    return Path(tmp.name)


# ── Unit tests ─────────────────────────────────────────────────────────────────

def test_jaccard_identical():
    assert _jaccard("hello world", "hello world") == 1.0


def test_jaccard_disjoint():
    assert _jaccard("foo bar", "baz qux") == 0.0


def test_jaccard_partial():
    score = _jaccard("hello world foo", "hello world bar")
    assert 0.4 < score < 0.7


def test_estimate_cost_known_model():
    cost = _estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == pytest.approx(18.0)


def test_estimate_cost_unknown_model():
    cost = _estimate_cost("unknown-model", 1_000_000, 0)
    assert cost == pytest.approx(3.0)


# ── Session analysis ───────────────────────────────────────────────────────────

def test_clean_session_no_findings():
    frames = [
        _make_llm_frame(0, output_tokens=200),
        _make_tool_frame(1, "bash", {"command": "ls"}, "file1.py\nfile2.py"),
        _make_llm_frame(2, output_tokens=150),
        _make_tool_frame(3, "read_file", {"path": "file1.py"}, "print('hello')"),
    ]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        assert isinstance(report, AutopsyReport)
        assert report.total_frames == 4
        assert report.llm_calls == 2
        assert report.tool_calls == 2
        # Clean session — no critical findings
        critical = [f for f in report.findings if f.severity == "critical"]
        assert len(critical) == 0
    finally:
        path.unlink()


def test_dead_loop_detected():
    # Same tool called 4 times with similar input, same output
    frames = [
        _make_tool_frame(0, "bash", {"command": "pytest tests/"}, "Error: ModuleNotFoundError"),
        _make_tool_frame(1, "bash", {"command": "pytest tests/"}, "Error: ModuleNotFoundError"),
        _make_tool_frame(2, "bash", {"command": "pytest tests/"}, "Error: ModuleNotFoundError"),
        _make_tool_frame(3, "bash", {"command": "pytest tests/"}, "Error: ModuleNotFoundError"),
    ]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        dead_loops = [f for f in report.findings if f.category == "dead_loop"]
        assert len(dead_loops) >= 1
        assert dead_loops[0].severity == "critical"
    finally:
        path.unlink()


def test_always_failing_detected():
    frames = [
        _make_tool_frame(0, "deploy", {"env": "prod"}, "Error: Permission denied"),
        _make_tool_frame(1, "deploy", {"env": "prod"}, "Error: Permission denied"),
        _make_tool_frame(2, "deploy", {"env": "staging"}, "Error: No such host"),
    ]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        failing = [f for f in report.findings if f.category == "always_failing"]
        assert len(failing) >= 1
        assert failing[0].detail["tool"] == "deploy"
    finally:
        path.unlink()


def test_redundant_read_detected():
    frames = [
        _make_tool_frame(0, "read_file", {"path": "config.yaml"}, "key: value"),
        _make_tool_frame(1, "bash", {"command": "ls"}, "config.yaml"),
        _make_tool_frame(2, "read_file", {"path": "config.yaml"}, "key: value"),
        _make_tool_frame(3, "read_file", {"path": "config.yaml"}, "key: value"),
    ]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        redundant = [f for f in report.findings if f.category == "redundant_read"]
        assert len(redundant) >= 1
        assert "config.yaml" in redundant[0].detail["path"]
        assert redundant[0].detail["read_count"] == 3
    finally:
        path.unlink()


def test_token_hog_detected():
    # Single LLM call with 80% of all tokens
    frames = [
        _make_llm_frame(0, output_tokens=100, input_tokens=100),
        _make_llm_frame(1, output_tokens=4000, input_tokens=4000),  # hog
        _make_llm_frame(2, output_tokens=100, input_tokens=100),
    ]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        hogs = [f for f in report.findings if f.category == "token_hog"]
        assert len(hogs) >= 1
        assert 1 in hogs[0].frame_ids
    finally:
        path.unlink()


def test_report_format_contains_stats():
    frames = [
        _make_llm_frame(0, output_tokens=500),
        _make_tool_frame(1, "bash", {"command": "ls"}, "file.py"),
    ]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        text = report.format()
        assert "frames" in text
        assert "tokens" in text
        assert "cost" in text
    finally:
        path.unlink()


def test_report_to_dict():
    frames = [_make_tool_frame(0, "bash", {"command": "ls"}, "ok")]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        d = report.to_dict()
        assert "session_id" in d
        assert "stats" in d
        assert "findings" in d
        assert isinstance(d["findings"], list)
    finally:
        path.unlink()


def test_nonexistent_file_raises():
    with pytest.raises(FileNotFoundError):
        analyze_session("/does/not/exist.prism")


def test_llm_ignored_detected():
    # LLM sandwiched between two identical tool calls → output likely ignored
    frames = [
        _make_tool_frame(0, "bash", {"command": "pip install flask"}, "Error: network timeout"),
        _make_llm_frame(1, output_tokens=200),
        _make_tool_frame(2, "bash", {"command": "pip install flask"}, "Error: network timeout"),
    ]
    path = _session_file(frames)
    try:
        report = analyze_session(path)
        ignored = [f for f in report.findings if f.category == "llm_ignored"]
        assert len(ignored) >= 1
    finally:
        path.unlink()
