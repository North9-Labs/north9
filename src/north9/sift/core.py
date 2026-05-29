"""Sift — load data files into SQLite, query with SQL."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any


class Sift:
    """In-memory SQLite database for querying structured data files."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._tables: dict[str, dict] = {}  # table_name -> {file, rows, columns}

    def load(self, path: str | Path, table: str | None = None, limit: int | None = None) -> str:
        """Load a CSV, JSON, or JSONL file into a SQLite table.

        Args:
            path: File path to load
            table: Table name (default: filename stem, sanitized)
            limit: Max rows to load (default: all)

        Returns: Summary string (table name, row count, column names)
        Raises: ValueError for unsupported formats or parse errors
        """
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        stem = path.stem.replace("-", "_").replace(" ", "_").lower()
        table = table or stem
        # Sanitize table name
        table = "".join(c if c.isalnum() or c == "_" else "_" for c in table)
        if table[0].isdigit():
            table = "t_" + table

        suffix = path.suffix.lower()
        if suffix == ".csv":
            rows, columns = _load_csv(path)
        elif suffix in (".json", ".jsonl", ".ndjson"):
            rows, columns = _load_json(path)
        else:
            raise ValueError(f"Unsupported format: {suffix}. Supported: .csv, .json, .jsonl")

        if limit:
            rows = rows[:limit]

        _create_table(self._conn, table, columns, rows)
        self._tables[table] = {"file": str(path), "rows": len(rows), "columns": columns}
        col_preview = ", ".join(columns[:5]) + ("…" if len(columns) > 5 else "")
        return f"Loaded {len(rows)} rows into '{table}' ({col_preview})"

    def query(self, sql: str, limit: int = 100) -> list[dict[str, Any]]:
        """Run a SQL SELECT query. Returns list of row dicts.

        A LIMIT is automatically appended if not present (default 100).
        """
        sql_lower = sql.strip().lower()
        if not sql_lower.startswith("select") and not sql_lower.startswith("with"):
            raise ValueError("Only SELECT queries are allowed")

        # Auto-add limit if missing
        if "limit" not in sql_lower:
            sql = sql.rstrip(";") + f" LIMIT {limit}"

        cur = self._conn.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def tables(self) -> list[dict]:
        """List all loaded tables."""
        return [
            {"table": name, "file": info["file"], "rows": info["rows"], "columns": info["columns"]}
            for name, info in self._tables.items()
        ]

    def schema(self, table: str) -> list[dict]:
        """Get column names and types for a table."""
        if table not in self._tables:
            raise ValueError(f"Unknown table: {table!r}. Loaded: {list(self._tables)}")
        # table validated against sanitized _tables keys — safe to interpolate
        rows = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [{"name": r[1], "type": r[2]} for r in rows]

    def sample(self, table: str, n: int = 5) -> list[dict[str, Any]]:
        """Get n sample rows from a table."""
        if table not in self._tables:
            raise ValueError(f"Unknown table: {table!r}. Loaded: {list(self._tables)}")
        n = min(max(1, int(n)), 1000)
        return self.query(f'SELECT * FROM "{table}" LIMIT {n}')

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Sift:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _load_csv(path: Path) -> tuple[list[list], list[str]]:
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return [], []
    headers = [
        h.strip().replace(" ", "_").replace("-", "_") or f"col{i}"
        for i, h in enumerate(rows[0])
    ]
    return rows[1:], headers


def _load_json(path: Path) -> tuple[list[list], list[str]]:
    suffix = path.suffix.lower()

    if suffix in (".jsonl", ".ndjson"):
        records = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            # Try common nested patterns
            for key in ("data", "items", "records", "results", "rows"):
                if key in data and isinstance(data[key], list):
                    records = data[key]
                    break
            else:
                records = [data]
        else:
            raise ValueError(
                f"JSON must be an array or object with a list field, got {type(data).__name__}"
            )

    if not records:
        return [], []

    # Collect all keys as columns
    all_keys: list[str] = []
    seen: set[str] = set()
    for rec in records[:100]:
        if isinstance(rec, dict):
            for k in rec:
                sanitized = k.replace(" ", "_").replace("-", "_")
                if sanitized not in seen:
                    all_keys.append(sanitized)
                    seen.add(sanitized)

    rows = [
        [str(rec.get(k, "")) if isinstance(rec, dict) else "" for k in all_keys]
        for rec in records
    ]
    return rows, all_keys


def _create_table(
    conn: sqlite3.Connection, table: str, columns: list[str], rows: list[list]
) -> None:
    col_defs = ", ".join(f'"{c}" TEXT' for c in columns)
    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
    if rows:
        placeholders = ", ".join(["?"] * len(columns))
        conn.executemany(
            f'INSERT INTO "{table}" VALUES ({placeholders})',
            [r[: len(columns)] + [""] * max(0, len(columns) - len(r)) for r in rows],
        )
    conn.commit()
