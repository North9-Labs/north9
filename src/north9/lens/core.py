"""Lens — agent observability. Record tool calls, query traces, compute costs."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".lens" / "traces.db"

# Token cost estimates (per 1M tokens) — update as pricing changes
_COST_PER_1M: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":           (15.0, 75.0),
    "claude-sonnet-4-6":         (3.0,  15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "gpt-4o":                    (2.5,  10.0),
    "gpt-4o-mini":               (0.15, 0.60),
}


@dataclass
class TraceRecord:
    id: str
    session_id: str
    tool_name: str
    input_json: str
    output: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    timestamp: str
    model: str
    error: str | None = None

    @classmethod
    def from_row(cls, row: tuple) -> TraceRecord:
        return cls(
            id=row[0], session_id=row[1], tool_name=row[2],
            input_json=row[3], output=row[4], tokens_in=row[5],
            tokens_out=row[6], latency_ms=row[7], timestamp=row[8],
            model=row[9], error=row[10],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "input": json.loads(self.input_json) if self.input_json else {},
            "output": self.output,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
            "model": self.model,
            "error": self.error,
        }


@dataclass
class Stats:
    total_calls: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    by_tool: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, dict] = field(default_factory=dict)
    errors: int = 0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total_calls if self.total_calls else 0.0

    def to_dict(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "by_tool": self.by_tool,
            "by_model": self.by_model,
            "errors": self.errors,
        }


class Tracer:
    """SQLite-backed tracer for agent tool calls."""

    def __init__(self, db_path: str | Path = _DEFAULT_DB, session_id: str | None = None):
        self.db_path = Path(db_path).expanduser()
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
        return self._conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                input_json TEXT,
                output TEXT,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0,
                timestamp TEXT NOT NULL,
                model TEXT DEFAULT '',
                error TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON traces(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool ON traces(tool_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON traces(timestamp)")
        conn.commit()

    def record(
        self,
        tool_name: str,
        input: dict[str, Any] | None = None,
        output: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        model: str = "",
        error: str | None = None,
        session_id: str | None = None,
    ) -> TraceRecord:
        rec = TraceRecord(
            id=str(uuid.uuid4()),
            session_id=session_id or self.session_id,
            tool_name=tool_name,
            input_json=json.dumps(input or {}),
            output=output[:10_000],  # cap stored output
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            timestamp=datetime.now(timezone.utc).isoformat(),
            model=model,
            error=error,
        )
        conn = self._connect()
        conn.execute(
            "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.session_id, rec.tool_name, rec.input_json,
             rec.output, rec.tokens_in, rec.tokens_out, rec.latency_ms,
             rec.timestamp, rec.model, rec.error),
        )
        conn.commit()
        return rec

    def query(
        self,
        session_id: str | None = None,
        tool_name: str | None = None,
        limit: int = 50,
    ) -> list[TraceRecord]:
        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if tool_name:
            where.append("tool_name = ?")
            params.append(tool_name)
        sql = "SELECT * FROM traces"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        return [TraceRecord.from_row(r) for r in conn.execute(sql, params).fetchall()]

    def stats(self, session_id: str | None = None) -> Stats:
        where = "WHERE session_id = ?" if session_id else ""
        params: list[Any] = [session_id] if session_id else []
        conn = self._connect()

        rows = conn.execute(
            f"SELECT tool_name, model, tokens_in, tokens_out, latency_ms, error "  # noqa: S608
            f"FROM traces {where}",
            params,
        ).fetchall()

        s = Stats()
        for tool_name, model, ti, to_, lat, err in rows:
            s.total_calls += 1
            s.total_tokens_in += ti
            s.total_tokens_out += to_
            s.total_latency_ms += lat
            if err:
                s.errors += 1
            s.by_tool[tool_name] = s.by_tool.get(tool_name, 0) + 1
            if model not in s.by_model:
                s.by_model[model] = {"calls": 0, "tokens_in": 0, "tokens_out": 0}
            s.by_model[model]["calls"] += 1
            s.by_model[model]["tokens_in"] += ti
            s.by_model[model]["tokens_out"] += to_

        # Estimate cost
        for model_name, mstats in s.by_model.items():
            rates = _COST_PER_1M.get(model_name)
            if rates:
                in_rate, out_rate = rates
                s.estimated_cost_usd += (mstats["tokens_in"] / 1_000_000) * in_rate
                s.estimated_cost_usd += (mstats["tokens_out"] / 1_000_000) * out_rate

        return s

    def sessions(self, limit: int = 20) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT session_id,
                   COUNT(*) as calls,
                   SUM(tokens_in + tokens_out) as tokens,
                   MIN(timestamp) as started,
                   MAX(timestamp) as last_seen
            FROM traces
            GROUP BY session_id
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {"session_id": r[0], "calls": r[1], "tokens": r[2],
             "started": r[3], "last_seen": r[4]}
            for r in rows
        ]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Tracer:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
