"""Comprehensive tests for Lens — agent observability package."""
from __future__ import annotations

import json

import pytest

from north9.lens.core import Stats, Tracer, TraceRecord

# ── Tracer tests ──────────────────────────────────────────────────────────────


def test_tracer_record_stores_correctly(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-1") as tracer:
        rec = tracer.record(
            tool_name="bash",
            input={"command": "ls"},
            output="file1.txt\nfile2.txt",
            tokens_in=10,
            tokens_out=20,
            latency_ms=123.4,
            model="claude-sonnet-4-6",
        )

    assert rec.tool_name == "bash"
    assert rec.session_id == "sess-1"
    assert rec.tokens_in == 10
    assert rec.tokens_out == 20
    assert rec.latency_ms == 123.4
    assert rec.model == "claude-sonnet-4-6"
    assert rec.error is None
    assert rec.id  # non-empty UUID
    assert rec.timestamp  # non-empty


def test_tracer_record_with_error(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-err") as tracer:
        rec = tracer.record(
            tool_name="read_file",
            error="FileNotFoundError: /tmp/nope.txt",
        )

    assert rec.error == "FileNotFoundError: /tmp/nope.txt"


def test_tracer_record_output_capped(tmp_path):
    db = tmp_path / "test.db"
    huge_output = "x" * 20_000
    with Tracer(db_path=db, session_id="sess-cap") as tracer:
        rec = tracer.record(tool_name="tool", output=huge_output)

    assert len(rec.output) == 10_000


def test_tracer_to_dict(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-dict") as tracer:
        rec = tracer.record(
            tool_name="write_file",
            input={"path": "/tmp/a.txt", "content": "hello"},
        )

    d = rec.to_dict()
    assert d["tool_name"] == "write_file"
    assert d["input"] == {"path": "/tmp/a.txt", "content": "hello"}
    assert "id" in d
    assert "timestamp" in d


def test_tracer_query_all(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-q") as tracer:
        tracer.record(tool_name="bash")
        tracer.record(tool_name="read_file")
        tracer.record(tool_name="write_file")
        results = tracer.query()

    assert len(results) == 3


def test_tracer_query_filter_by_session_id(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-a") as tracer:
        tracer.record(tool_name="bash")

    with Tracer(db_path=db, session_id="sess-b") as tracer:
        tracer.record(tool_name="bash")
        tracer.record(tool_name="bash")

    # Query only sess-a
    with Tracer(db_path=db, session_id="sess-a") as tracer:
        results = tracer.query(session_id="sess-a")
        assert len(results) == 1
        assert results[0].session_id == "sess-a"

        results_b = tracer.query(session_id="sess-b")
        assert len(results_b) == 2
        assert all(r.session_id == "sess-b" for r in results_b)


def test_tracer_query_filter_by_tool_name(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-tool") as tracer:
        tracer.record(tool_name="bash")
        tracer.record(tool_name="bash")
        tracer.record(tool_name="read_file")
        tracer.record(tool_name="write_file")

        bash_results = tracer.query(tool_name="bash")
        assert len(bash_results) == 2
        assert all(r.tool_name == "bash" for r in bash_results)

        read_results = tracer.query(tool_name="read_file")
        assert len(read_results) == 1


def test_tracer_query_limit(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-lim") as tracer:
        for _ in range(10):
            tracer.record(tool_name="bash")

        results = tracer.query(limit=3)
        assert len(results) == 3


def test_tracer_stats_totals(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-stats") as tracer:
        tracer.record(tool_name="bash", tokens_in=100, tokens_out=200, latency_ms=50.0,
                      model="claude-sonnet-4-6")
        tracer.record(tool_name="read_file", tokens_in=50, tokens_out=100, latency_ms=25.0,
                      model="claude-sonnet-4-6")
        tracer.record(tool_name="bash", tokens_in=200, tokens_out=300, latency_ms=75.0,
                      model="gpt-4o")

        s = tracer.stats(session_id="sess-stats")

    assert s.total_calls == 3
    assert s.total_tokens_in == 350
    assert s.total_tokens_out == 600
    assert s.total_latency_ms == 150.0
    assert s.avg_latency_ms == pytest.approx(50.0, 0.01)
    assert s.errors == 0


def test_tracer_stats_avg_latency(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-lat") as tracer:
        tracer.record(tool_name="bash", latency_ms=100.0)
        tracer.record(tool_name="bash", latency_ms=200.0)
        tracer.record(tool_name="bash", latency_ms=300.0)

        s = tracer.stats(session_id="sess-lat")

    assert s.avg_latency_ms == pytest.approx(200.0, 0.01)


def test_tracer_stats_avg_latency_empty():
    """avg_latency_ms is 0 when total_calls is 0."""
    s = Stats()
    assert s.avg_latency_ms == 0.0


def test_tracer_stats_by_tool(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-by-tool") as tracer:
        for _ in range(3):
            tracer.record(tool_name="bash")
        tracer.record(tool_name="read_file")
        tracer.record(tool_name="read_file")

        s = tracer.stats(session_id="sess-by-tool")

    assert s.by_tool["bash"] == 3
    assert s.by_tool["read_file"] == 2


def test_tracer_stats_by_model(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-by-model") as tracer:
        tracer.record(tool_name="bash", model="claude-sonnet-4-6",
                      tokens_in=100, tokens_out=200)
        tracer.record(tool_name="bash", model="gpt-4o",
                      tokens_in=50, tokens_out=100)

        s = tracer.stats(session_id="sess-by-model")

    assert "claude-sonnet-4-6" in s.by_model
    assert "gpt-4o" in s.by_model
    assert s.by_model["claude-sonnet-4-6"]["tokens_in"] == 100
    assert s.by_model["gpt-4o"]["tokens_out"] == 100


def test_tracer_stats_estimated_cost_nonnegative(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-cost") as tracer:
        tracer.record(tool_name="bash", model="claude-sonnet-4-6",
                      tokens_in=1000, tokens_out=2000)
        tracer.record(tool_name="bash", model="gpt-4o-mini",
                      tokens_in=500, tokens_out=1000)

        s = tracer.stats(session_id="sess-cost")

    assert s.estimated_cost_usd >= 0.0


def test_tracer_stats_estimated_cost_known_model(tmp_path):
    """1M input tokens at claude-sonnet-4-6 rate = $3.00."""
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-cost2") as tracer:
        tracer.record(tool_name="bash", model="claude-sonnet-4-6",
                      tokens_in=1_000_000, tokens_out=0)
        s = tracer.stats(session_id="sess-cost2")

    assert s.estimated_cost_usd == pytest.approx(3.0, rel=1e-3)


def test_tracer_stats_errors(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-errs") as tracer:
        tracer.record(tool_name="bash")
        tracer.record(tool_name="bash", error="timeout")
        tracer.record(tool_name="bash", error="permission denied")

        s = tracer.stats(session_id="sess-errs")

    assert s.errors == 2


def test_tracer_stats_all_sessions(tmp_path):
    """stats() without session_id aggregates all sessions."""
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-x") as tracer:
        tracer.record(tool_name="bash", tokens_in=100)

    with Tracer(db_path=db, session_id="sess-y") as tracer:
        tracer.record(tool_name="bash", tokens_in=200)
        s = tracer.stats()

    assert s.total_calls == 2
    assert s.total_tokens_in == 300


def test_tracer_sessions(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-a") as tracer:
        tracer.record(tool_name="bash")
        tracer.record(tool_name="bash")

    with Tracer(db_path=db, session_id="sess-b") as tracer:
        tracer.record(tool_name="read_file")
        sessions = tracer.sessions()

    session_ids = [s["session_id"] for s in sessions]
    assert "sess-a" in session_ids
    assert "sess-b" in session_ids

    sess_a = next(s for s in sessions if s["session_id"] == "sess-a")
    assert sess_a["calls"] == 2


def test_tracer_sessions_limit(tmp_path):
    db = tmp_path / "test.db"
    for i in range(5):
        with Tracer(db_path=db, session_id=f"sess-{i}") as tracer:
            tracer.record(tool_name="bash")

    with Tracer(db_path=db) as tracer:
        sessions = tracer.sessions(limit=3)
    assert len(sessions) == 3


def test_tracer_context_manager(tmp_path):
    db = tmp_path / "test.db"
    with Tracer(db_path=db, session_id="sess-ctx") as tracer:
        tracer.record(tool_name="bash")
        assert tracer._conn is not None
    # After __exit__, connection should be closed
    assert tracer._conn is None


def test_trace_record_from_row():
    row = (
        "id-1", "sess-1", "bash", '{"cmd": "ls"}', "file.txt",
        10, 20, 50.0, "2025-01-01T00:00:00+00:00", "gpt-4o", None,
    )
    rec = TraceRecord.from_row(row)
    assert rec.id == "id-1"
    assert rec.tool_name == "bash"
    assert rec.tokens_in == 10


# ── MCP tool tests ────────────────────────────────────────────────────────────


@pytest.fixture()
def mcp_tracer(tmp_path):
    """Set up lens.mcp module with a temporary tracer."""
    import north9.lens.mcp as m

    db = tmp_path / "mcp_test.db"
    old_tracer = m._tracer
    old_session = m._session_id

    m._tracer = Tracer(db_path=db, session_id="test-session")
    m._session_id = "test-session"

    yield m

    m._tracer.close()
    m._tracer = old_tracer
    m._session_id = old_session


def test_mcp_lens_session_id(mcp_tracer):
    result = mcp_tracer.lens_session_id()
    assert result == "test-session"


def test_mcp_lens_record(mcp_tracer):
    result = mcp_tracer.lens_record(
        tool_name="bash",
        output="hello world",
        tokens_in=10,
        tokens_out=5,
        latency_ms=100.0,
        model="claude-sonnet-4-6",
    )
    data = json.loads(result)
    assert data["recorded"] is True
    assert data["tool_name"] == "bash"
    assert data["tokens_in"] == 10
    assert data["tokens_out"] == 5
    assert data["session_id"] == "test-session"
    assert "id" in data


def test_mcp_lens_record_with_error(mcp_tracer):
    result = mcp_tracer.lens_record(
        tool_name="read_file",
        error="file not found",
    )
    data = json.loads(result)
    assert data["recorded"] is True


def test_mcp_lens_stats(mcp_tracer):
    mcp_tracer.lens_record("bash", tokens_in=100, tokens_out=200, model="claude-sonnet-4-6")
    mcp_tracer.lens_record("read_file", tokens_in=50, tokens_out=30)

    result = mcp_tracer.lens_stats()
    data = json.loads(result)
    assert data["total_calls"] == 2
    assert data["total_tokens_in"] == 150
    assert data["total_tokens_out"] == 230
    assert "by_tool" in data
    assert data["by_tool"]["bash"] == 1
    assert data["estimated_cost_usd"] >= 0.0


def test_mcp_lens_stats_empty_session(mcp_tracer):
    """lens_stats with no records returns zero totals."""
    result = mcp_tracer.lens_stats()
    data = json.loads(result)
    assert data["total_calls"] == 0
    assert data["estimated_cost_usd"] == 0.0


def test_mcp_lens_query(mcp_tracer):
    mcp_tracer.lens_record("bash", output="result1")
    mcp_tracer.lens_record("read_file", output="result2")
    mcp_tracer.lens_record("write_file", output="result3")

    result = mcp_tracer.lens_query()
    records = json.loads(result)
    assert len(records) == 3
    tool_names = {r["tool_name"] for r in records}
    assert tool_names == {"bash", "read_file", "write_file"}


def test_mcp_lens_query_filter_tool(mcp_tracer):
    mcp_tracer.lens_record("bash")
    mcp_tracer.lens_record("bash")
    mcp_tracer.lens_record("read_file")

    result = mcp_tracer.lens_query(tool_name="bash")
    records = json.loads(result)
    assert len(records) == 2
    assert all(r["tool_name"] == "bash" for r in records)


def test_mcp_lens_query_limit(mcp_tracer):
    for _ in range(10):
        mcp_tracer.lens_record("bash")

    result = mcp_tracer.lens_query(limit=3)
    records = json.loads(result)
    assert len(records) == 3


def test_mcp_lens_sessions(mcp_tracer):
    mcp_tracer.lens_record("bash")
    mcp_tracer.lens_record("read_file")

    result = mcp_tracer.lens_sessions()
    sessions = json.loads(result)
    assert len(sessions) >= 1
    sess = next(s for s in sessions if s["session_id"] == "test-session")
    assert sess["calls"] == 2


def test_stats_to_dict():
    s = Stats(
        total_calls=5,
        total_tokens_in=1000,
        total_tokens_out=2000,
        total_latency_ms=500.0,
        estimated_cost_usd=0.05,
        by_tool={"bash": 3, "read_file": 2},
        errors=1,
    )
    d = s.to_dict()
    assert d["total_calls"] == 5
    assert d["avg_latency_ms"] == 100.0
    assert d["by_tool"]["bash"] == 3
    assert d["errors"] == 1
