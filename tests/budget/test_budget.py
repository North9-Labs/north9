"""Tests for the Budget package."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import north9.budget.mcp as mcp_module
from north9.budget.core import Budget, BudgetExceeded

# ---------------------------------------------------------------------------
# Core tests
# ---------------------------------------------------------------------------


def test_record_stores_usage(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="s1")
    status = b.record(tokens_in=100, tokens_out=50)
    assert status.tokens_used == 150
    assert status.calls == 1


def test_status_returns_correct_totals(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="s1")
    b.record(tokens_in=100, tokens_out=50)
    b.record(tokens_in=200, tokens_out=100)
    status = b.status()
    assert status.tokens_used == 450
    assert status.calls == 2


def test_record_raises_budget_exceeded_tokens(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="s1", tokens_limit=100)
    with pytest.raises(BudgetExceeded):
        b.record(tokens_in=80, tokens_out=30)


def test_record_raises_budget_exceeded_cost(tmp_path: Path) -> None:
    # claude-sonnet-4-6: $3/M in, $15/M out
    # 1M input tokens = $3.00 — set limit at $1.00
    b = Budget(
        db_path=tmp_path / "test.db",
        session_id="s1",
        cost_limit_usd=0.001,
    )
    with pytest.raises(BudgetExceeded):
        # 1000 in + 0 out at $3/M = $0.003 — over $0.001 limit
        b.record(tokens_in=1000, tokens_out=0, model="claude-sonnet-4-6")


def test_sessions_returns_list(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="s1")
    b.record(tokens_in=100, tokens_out=50)
    sessions = b.sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["tokens"] == 150
    assert sessions[0]["calls"] == 1


def test_sessions_multiple(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    b1 = Budget(db_path=db, session_id="s1")
    b1.record(tokens_in=100, tokens_out=50)
    b1.close()

    b2 = Budget(db_path=db, session_id="s2")
    b2.record(tokens_in=200, tokens_out=100)
    b2.close()

    b3 = Budget(db_path=db, session_id="s1")
    sessions = b3.sessions(limit=10)
    assert len(sessions) == 2


def test_reset_clears_session_usage(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="s1")
    b.record(tokens_in=100, tokens_out=50)
    assert b.status().calls == 1
    b.reset()
    status = b.status()
    assert status.tokens_used == 0
    assert status.calls == 0


def test_reset_does_not_affect_other_sessions(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    b1 = Budget(db_path=db, session_id="s1")
    b1.record(tokens_in=100, tokens_out=50)

    b2 = Budget(db_path=db, session_id="s2")
    b2.record(tokens_in=200, tokens_out=100)

    b1.reset()
    assert b1.status().calls == 0
    assert b2.status().calls == 1


def test_budget_status_properties(tmp_path: Path) -> None:
    b = Budget(
        db_path=tmp_path / "test.db",
        session_id="s1",
        tokens_limit=1000,
        cost_limit_usd=1.0,
    )
    b.record(tokens_in=300, tokens_out=100)
    status = b.status()
    assert status.tokens_remaining == 600
    assert status.tokens_pct == pytest.approx(40.0)
    assert not status.is_over_budget()


def test_budget_context_manager(tmp_path: Path) -> None:
    with Budget(db_path=tmp_path / "test.db", session_id="ctx") as b:
        b.record(tokens_in=10, tokens_out=5)
        assert b.status().calls == 1
    # after __exit__, connection should be closed
    assert b._conn is None


def test_budget_status_format(tmp_path: Path) -> None:
    b = Budget(
        db_path=tmp_path / "test.db",
        session_id="fmt1",
        tokens_limit=1000,
        cost_limit_usd=1.0,
    )
    b.record(tokens_in=100, tokens_out=50, model="claude-sonnet-4-6")
    text = b.status().format()
    assert "fmt1" in text
    assert "150" in text


def test_budget_to_dict(tmp_path: Path) -> None:
    b = Budget(
        db_path=tmp_path / "test.db",
        session_id="d1",
        tokens_limit=1000,
        cost_limit_usd=2.0,
    )
    b.record(tokens_in=100, tokens_out=50)
    d = b.status().to_dict()
    assert d["tokens_used"] == 150
    assert d["tokens_limit"] == 1000
    assert d["over_budget"] is False
    assert "cost_usd" in d


# ---------------------------------------------------------------------------
# BudgetedClient tests
# ---------------------------------------------------------------------------


def _mock_client(input_tokens: int = 100, output_tokens: int = 50, model: str = "claude-sonnet-4-6") -> MagicMock:
    """Build a minimal mock Anthropic client."""
    client = MagicMock()
    response = MagicMock()
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    client.messages.create.return_value = response
    return client


def test_budgeted_client_tracks_usage(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="bc1")
    client = _mock_client()
    wrapped = b.wrap(client)
    wrapped.messages.create(model="claude-sonnet-4-6", messages=[])
    status = b.status()
    assert status.tokens_used == 150
    assert status.calls == 1


def test_budgeted_client_passes_through_response(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="bc2")
    client = _mock_client()
    wrapped = b.wrap(client)
    resp = wrapped.messages.create(model="claude-sonnet-4-6", messages=[])
    assert resp is client.messages.create.return_value


def test_budgeted_client_multiple_calls(tmp_path: Path) -> None:
    b = Budget(db_path=tmp_path / "test.db", session_id="bc3")
    client = _mock_client(input_tokens=200, output_tokens=100)
    wrapped = b.wrap(client)
    wrapped.messages.create(model="claude-sonnet-4-6", messages=[])
    wrapped.messages.create(model="claude-sonnet-4-6", messages=[])
    assert b.status().calls == 2
    assert b.status().tokens_used == 600


def test_budgeted_client_no_usage_attr(tmp_path: Path) -> None:
    """Client response without .usage should not crash."""
    b = Budget(db_path=tmp_path / "test.db", session_id="bc4")
    client = MagicMock()
    response = MagicMock(spec=[])  # no .usage attribute
    client.messages.create.return_value = response
    wrapped = b.wrap(client)
    resp = wrapped.messages.create(model="claude-sonnet-4-6", messages=[])
    assert resp is response
    assert b.status().calls == 0


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def mcp_budget(tmp_path: Path) -> Budget:
    """Wire a fresh Budget into the MCP module and restore on teardown."""
    b = Budget(db_path=tmp_path / "mcp.db", session_id="mcp1")
    original = mcp_module._budget
    mcp_module._budget = b
    yield b
    mcp_module._budget = original
    b.close()


def test_mcp_budget_status_returns_string(mcp_budget: Budget) -> None:
    result = mcp_module.budget_status()
    assert isinstance(result, str)
    assert "mcp1" in result


def test_mcp_budget_record_returns_string(mcp_budget: Budget) -> None:
    result = mcp_module.budget_record(tokens_in=100, tokens_out=50)
    assert isinstance(result, str)
    assert "150" in result


def test_mcp_budget_record_over_limit_returns_warning(mcp_budget: Budget) -> None:
    mcp_budget.tokens_limit = 10
    result = mcp_module.budget_record(tokens_in=100, tokens_out=50)
    assert isinstance(result, str)
    assert "WARNING" in result


def test_mcp_budget_set_limit_changes_limits(mcp_budget: Budget) -> None:
    result = mcp_module.budget_set_limit(tokens=50000, cost_usd=2.50)
    assert isinstance(result, str)
    assert mcp_budget.tokens_limit == 50000
    assert mcp_budget.cost_limit_usd == pytest.approx(2.50)
    assert "50,000" in result


def test_mcp_budget_set_limit_clear(mcp_budget: Budget) -> None:
    mcp_budget.tokens_limit = 9999
    mcp_module.budget_set_limit(tokens=0, cost_usd=0.0)
    assert mcp_budget.tokens_limit is None
    assert mcp_budget.cost_limit_usd is None


def test_mcp_budget_sessions_returns_string(mcp_budget: Budget) -> None:
    mcp_budget.record(tokens_in=100, tokens_out=50)
    result = mcp_module.budget_sessions()
    assert isinstance(result, str)
    assert "mcp1" in result


def test_mcp_budget_sessions_empty(mcp_budget: Budget) -> None:
    result = mcp_module.budget_sessions()
    assert "No sessions" in result


def test_mcp_budget_reset_clears(mcp_budget: Budget) -> None:
    mcp_budget.record(tokens_in=100, tokens_out=50)
    assert mcp_budget.status().calls == 1
    result = mcp_module.budget_reset()
    assert isinstance(result, str)
    assert mcp_budget.status().calls == 0
    assert "reset" in result.lower()
