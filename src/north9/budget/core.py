"""Budget — token and cost enforcement for AI agents."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".budget" / "usage.db"

_COST_PER_1M: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":              (15.0, 75.0),
    "claude-sonnet-4-6":            (3.0,  15.0),
    "claude-haiku-4-5-20251001":    (0.80, 4.0),
    "gpt-4o":                       (2.5,  10.0),
    "gpt-4o-mini":                  (0.15, 0.60),
}


class BudgetExceeded(Exception):
    """Raised when a hard token or cost limit is hit."""
    pass


@dataclass
class BudgetStatus:
    session_id: str
    tokens_used: int
    tokens_limit: int | None
    cost_usd: float
    cost_limit_usd: float | None
    calls: int

    @property
    def tokens_remaining(self) -> int | None:
        return (self.tokens_limit - self.tokens_used) if self.tokens_limit else None

    @property
    def cost_remaining(self) -> float | None:
        return (self.cost_limit_usd - self.cost_usd) if self.cost_limit_usd else None

    @property
    def tokens_pct(self) -> float | None:
        return (self.tokens_used / self.tokens_limit * 100) if self.tokens_limit else None

    @property
    def cost_pct(self) -> float | None:
        return (self.cost_usd / self.cost_limit_usd * 100) if self.cost_limit_usd else None

    def is_over_budget(self) -> bool:
        if self.tokens_limit and self.tokens_used >= self.tokens_limit:
            return True
        if self.cost_limit_usd and self.cost_usd >= self.cost_limit_usd:
            return True
        return False

    def format(self) -> str:
        lines = [f"Budget — session {self.session_id}"]
        lines.append(f"  Calls: {self.calls}")
        if self.tokens_limit:
            pct = round(self.tokens_pct, 1)  # type: ignore[arg-type]
            lines.append(f"  Tokens: {self.tokens_used:,} / {self.tokens_limit:,}  ({pct}%)")
        else:
            lines.append(f"  Tokens: {self.tokens_used:,} (no limit)")
        if self.cost_limit_usd:
            pct = round(self.cost_pct, 1)  # type: ignore[arg-type]
            lines.append(f"  Cost:   ${self.cost_usd:.4f} / ${self.cost_limit_usd:.2f}  ({pct}%)")
        else:
            lines.append(f"  Cost:   ${self.cost_usd:.4f} (no limit)")
        if self.is_over_budget():
            lines.append("  BUDGET EXCEEDED")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tokens_used": self.tokens_used,
            "tokens_limit": self.tokens_limit,
            "tokens_remaining": self.tokens_remaining,
            "cost_usd": round(self.cost_usd, 6),
            "cost_limit_usd": self.cost_limit_usd,
            "cost_remaining": round(self.cost_remaining, 6) if self.cost_remaining is not None else None,
            "calls": self.calls,
            "over_budget": self.is_over_budget(),
        }


class Budget:
    """Track and enforce token/cost budgets per session."""

    def __init__(
        self,
        db_path: str | Path = _DEFAULT_DB,
        session_id: str | None = None,
        tokens_limit: int | None = None,
        cost_limit_usd: float | None = None,
    ):
        self.db_path = Path(db_path).expanduser()
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.tokens_limit = tokens_limit
        self.cost_limit_usd = cost_limit_usd
        self._conn: sqlite3.Connection | None = None
        try:
            self._init_db()
        except Exception:
            if self._conn:
                self._conn.close()
                self._conn = None
            raise

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
        return self._conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                tokens_in INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON usage(session_id)")
        conn.commit()

    def record(
        self,
        tokens_in: int,
        tokens_out: int,
        model: str = "",
    ) -> BudgetStatus:
        """Record usage. Raises BudgetExceeded if over limit."""
        cost = 0.0
        rates = _COST_PER_1M.get(model)
        if rates:
            in_rate, out_rate = rates
            cost = (tokens_in / 1_000_000) * in_rate + (tokens_out / 1_000_000) * out_rate

        conn = self._connect()
        conn.execute(
            "INSERT INTO usage VALUES (?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                self.session_id,
                model,
                tokens_in,
                tokens_out,
                cost,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

        status = self.status()
        if status.is_over_budget():
            raise BudgetExceeded(
                f"Budget exceeded: {status.tokens_used} tokens used "
                f"(limit: {status.tokens_limit}), "
                f"${status.cost_usd:.4f} spent (limit: ${status.cost_limit_usd})"
            )
        return status

    def status(self) -> BudgetStatus:
        conn = self._connect()
        rows = conn.execute(
            "SELECT tokens_in, tokens_out, cost_usd FROM usage WHERE session_id = ?",
            (self.session_id,),
        ).fetchall()
        tokens_used = sum(r[0] + r[1] for r in rows)
        cost_usd = sum(r[2] for r in rows)
        return BudgetStatus(
            session_id=self.session_id,
            tokens_used=tokens_used,
            tokens_limit=self.tokens_limit,
            cost_usd=cost_usd,
            cost_limit_usd=self.cost_limit_usd,
            calls=len(rows),
        )

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute("""
            SELECT session_id,
                   SUM(tokens_in + tokens_out) as tokens,
                   SUM(cost_usd) as cost,
                   COUNT(*) as calls,
                   MIN(timestamp) as started
            FROM usage
            GROUP BY session_id
            ORDER BY started DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [
            {"session_id": r[0], "tokens": r[1], "cost_usd": round(r[2], 6),
             "calls": r[3], "started": r[4]}
            for r in rows
        ]

    def reset(self) -> None:
        """Clear all usage records for this session."""
        conn = self._connect()
        conn.execute("DELETE FROM usage WHERE session_id = ?", (self.session_id,))
        conn.commit()

    def wrap(self, client: Any) -> BudgetedClient:
        """Wrap an Anthropic client to automatically track usage."""
        return BudgetedClient(client=client, budget=self)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Budget:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class BudgetedClient:
    """Wraps an Anthropic client to auto-track token usage against a Budget."""

    def __init__(self, client: Any, budget: Budget):
        self._client = client
        self._budget = budget
        self.messages = _BudgetedMessages(client.messages, budget)


class _BudgetedMessages:
    def __init__(self, messages: Any, budget: Budget):
        self._messages = messages
        self._budget = budget

    def create(self, **kwargs: Any) -> Any:
        response = self._messages.create(**kwargs)
        model = kwargs.get("model", "")
        if hasattr(response, "usage"):
            self._budget.record(
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                model=model,
            )
        return response
