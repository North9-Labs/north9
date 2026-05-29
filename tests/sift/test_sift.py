"""Tests for Sift core and MCP tools."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import north9.sift.mcp as mcp_module
from north9.sift.core import Sift, _load_csv, _load_json

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def csv_file(tmp_path: Path) -> Path:
    p = tmp_path / "sales.csv"
    p.write_text("name,revenue,category\nAlice,1000,A\nBob,2000,B\nCarol,500,A\n", encoding="utf-8")
    return p


@pytest.fixture()
def json_file(tmp_path: Path) -> Path:
    p = tmp_path / "items.json"
    records = [{"id": "1", "value": "10"}, {"id": "2", "value": "20"}, {"id": "3", "value": "30"}]
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


@pytest.fixture()
def jsonl_file(tmp_path: Path) -> Path:
    p = tmp_path / "events.jsonl"
    lines = [
        '{"ts": "2024-01-01", "event": "click"}',
        '{"ts": "2024-01-02", "event": "view"}',
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture()
def nested_json_file(tmp_path: Path) -> Path:
    p = tmp_path / "response.json"
    data = {"meta": {"total": 2}, "data": [{"x": "1"}, {"x": "2"}]}
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.fixture()
def fresh_sift() -> Sift:
    s = Sift()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def reset_mcp_sift(tmp_path: Path):
    """Replace the module-level _sift with a fresh instance for each test."""
    original = mcp_module._sift
    mcp_module._sift = Sift()
    yield
    mcp_module._sift.close()
    mcp_module._sift = original


# ── _load_csv ─────────────────────────────────────────────────────────────────

def test_load_csv_parses_correctly(csv_file: Path):
    rows, cols = _load_csv(csv_file)
    assert cols == ["name", "revenue", "category"]
    assert len(rows) == 3
    assert rows[0] == ["Alice", "1000", "A"]


def test_load_csv_empty_file(tmp_path: Path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    rows, cols = _load_csv(p)
    assert rows == []
    assert cols == []


# ── _load_json ────────────────────────────────────────────────────────────────

def test_load_json_array(json_file: Path):
    rows, cols = _load_json(json_file)
    assert cols == ["id", "value"]
    assert len(rows) == 3
    assert rows[0] == ["1", "10"]


def test_load_json_jsonl(jsonl_file: Path):
    rows, cols = _load_json(jsonl_file)
    assert "ts" in cols
    assert "event" in cols
    assert len(rows) == 2


def test_load_json_nested_data_key(nested_json_file: Path):
    rows, cols = _load_json(nested_json_file)
    assert cols == ["x"]
    assert len(rows) == 2


# ── Sift.load ─────────────────────────────────────────────────────────────────

def test_sift_load_csv(fresh_sift: Sift, csv_file: Path):
    result = fresh_sift.load(csv_file)
    assert "sales" in result
    assert "3" in result


def test_sift_load_json(fresh_sift: Sift, json_file: Path):
    result = fresh_sift.load(json_file)
    assert "items" in result
    assert "3" in result


def test_sift_load_returns_summary(fresh_sift: Sift, csv_file: Path):
    result = fresh_sift.load(csv_file)
    assert "Loaded" in result
    assert "rows" in result
    assert "name" in result


def test_sift_load_nonexistent_raises(fresh_sift: Sift):
    with pytest.raises(FileNotFoundError):
        fresh_sift.load("/nonexistent/path/file.csv")


def test_sift_load_unsupported_format(fresh_sift: Sift, tmp_path: Path):
    p = tmp_path / "data.parquet"
    p.write_bytes(b"PAR1")
    with pytest.raises(ValueError, match="Unsupported format"):
        fresh_sift.load(p)


def test_sift_load_reloading_replaces_table(fresh_sift: Sift, tmp_path: Path):
    p = tmp_path / "data.csv"
    p.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    fresh_sift.load(p, table="mytable")
    rows_first = fresh_sift.query("SELECT * FROM mytable")
    assert len(rows_first) == 2

    p.write_text("a,b\n10,20\n", encoding="utf-8")
    fresh_sift.load(p, table="mytable")
    rows_second = fresh_sift.query("SELECT * FROM mytable")
    assert len(rows_second) == 1
    assert rows_second[0]["a"] == "10"


# ── Sift.query ────────────────────────────────────────────────────────────────

def test_sift_query_select(fresh_sift: Sift, csv_file: Path):
    fresh_sift.load(csv_file)
    rows = fresh_sift.query("SELECT * FROM sales")
    assert len(rows) == 3
    assert rows[0]["name"] == "Alice"


def test_sift_query_where(fresh_sift: Sift, csv_file: Path):
    fresh_sift.load(csv_file)
    rows = fresh_sift.query("SELECT * FROM sales WHERE category = 'A'")
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"Alice", "Carol"}


def test_sift_query_auto_limit(fresh_sift: Sift, tmp_path: Path):
    p = tmp_path / "big.csv"
    header = "id\n"
    data = "\n".join(str(i) for i in range(200))
    p.write_text(header + data, encoding="utf-8")
    fresh_sift.load(p)
    # default limit=100 should kick in
    rows = fresh_sift.query("SELECT * FROM big")
    assert len(rows) == 100


def test_sift_query_rejects_non_select(fresh_sift: Sift, csv_file: Path):
    fresh_sift.load(csv_file)
    with pytest.raises(ValueError, match="Only SELECT"):
        fresh_sift.query("DROP TABLE sales")


# ── Sift.tables / schema / sample ─────────────────────────────────────────────

def test_sift_tables(fresh_sift: Sift, csv_file: Path, json_file: Path):
    fresh_sift.load(csv_file)
    fresh_sift.load(json_file)
    tables = fresh_sift.tables()
    names = {t["table"] for t in tables}
    assert "sales" in names
    assert "items" in names


def test_sift_schema(fresh_sift: Sift, csv_file: Path):
    fresh_sift.load(csv_file)
    schema = fresh_sift.schema("sales")
    names = [c["name"] for c in schema]
    assert names == ["name", "revenue", "category"]


def test_sift_sample(fresh_sift: Sift, csv_file: Path):
    fresh_sift.load(csv_file)
    rows = fresh_sift.sample("sales", n=2)
    assert len(rows) == 2


# ── MCP tools ─────────────────────────────────────────────────────────────────

def test_mcp_sift_load(tmp_path: Path):
    p = tmp_path / "test.csv"
    p.write_text("col1,col2\nfoo,bar\nbaz,qux\n", encoding="utf-8")
    result = mcp_module.sift_load(str(p))
    assert "Loaded" in result
    assert "2" in result


def test_mcp_sift_query_returns_json(tmp_path: Path):
    p = tmp_path / "test.csv"
    p.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    mcp_module.sift_load(str(p))
    result = mcp_module.sift_query("SELECT * FROM test")
    data = json.loads(result)
    assert len(data) == 2
    assert data[0]["x"] == "1"


def test_mcp_sift_query_empty_returns_empty_list(tmp_path: Path):
    p = tmp_path / "test.csv"
    p.write_text("x,y\n1,2\n", encoding="utf-8")
    mcp_module.sift_load(str(p))
    result = mcp_module.sift_query("SELECT * FROM test WHERE x = '999'")
    assert result == "[]"


def test_mcp_sift_tables(tmp_path: Path):
    p = tmp_path / "test.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    mcp_module.sift_load(str(p))
    result = mcp_module.sift_tables()
    data = json.loads(result)
    assert any(t["table"] == "test" for t in data)


def test_mcp_sift_schema(tmp_path: Path):
    p = tmp_path / "test.csv"
    p.write_text("alpha,beta\n1,2\n", encoding="utf-8")
    mcp_module.sift_load(str(p))
    result = mcp_module.sift_schema("test")
    data = json.loads(result)
    names = [c["name"] for c in data]
    assert "alpha" in names
    assert "beta" in names


def test_mcp_sift_sample(tmp_path: Path):
    p = tmp_path / "test.csv"
    p.write_text("v\n10\n20\n30\n40\n50\n", encoding="utf-8")
    mcp_module.sift_load(str(p))
    result = mcp_module.sift_sample("test", n=3)
    data = json.loads(result)
    assert len(data) == 3


def test_mcp_sift_query_non_select_returns_error(tmp_path: Path):
    p = tmp_path / "test.csv"
    p.write_text("a\n1\n", encoding="utf-8")
    mcp_module.sift_load(str(p))
    result = mcp_module.sift_query("DROP TABLE test")
    assert result.startswith("Error:")


def test_mcp_sift_tables_empty():
    result = mcp_module.sift_tables()
    assert "No tables" in result
