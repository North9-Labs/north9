"""Tests for Index — semantic memory store."""
from __future__ import annotations

import pytest

import north9.index.mcp as mcp_module
from north9.index.core import Index

# ── Core Index tests ───────────────────────────────────────────────────────────


@pytest.fixture()
def idx(tmp_path):
    db = tmp_path / "test.db"
    with Index(db) as index:
        yield index


def test_add_returns_string_id(idx):
    chunk_id = idx.add("The quick brown fox jumps over the lazy dog")
    assert isinstance(chunk_id, str)
    assert len(chunk_id) > 0


def test_search_finds_content_by_keyword(idx):
    idx.add("SQLite FTS5 supports BM25 ranking for full-text search")
    idx.add("Python asyncio event loop tutorial")
    results = idx.search("BM25 ranking")
    assert len(results) >= 1
    contents = [r.chunk.content for r in results]
    assert any("BM25" in c for c in contents)


def test_search_returns_empty_for_no_match(idx):
    idx.add("The weather today is sunny and warm")
    results = idx.search("quantum entanglement superconductor")
    assert results == []


def test_search_with_source_filter(idx):
    idx.add("auth bug in login flow", source="projectA")
    idx.add("auth issue in signup", source="projectB")
    results = idx.search("auth", source="projectA")
    assert len(results) >= 1
    for r in results:
        assert r.chunk.source == "projectA"


def test_list_returns_recent_chunks(idx):
    idx.add("first chunk")
    idx.add("second chunk")
    idx.add("third chunk")
    chunks = idx.list()
    assert len(chunks) == 3


def test_list_filters_by_source(idx):
    idx.add("belongs to A", source="project-a")
    idx.add("belongs to B", source="project-b")
    idx.add("also belongs to A", source="project-a")
    chunks = idx.list(source="project-a")
    assert len(chunks) == 2
    assert all(c.source == "project-a" for c in chunks)


def test_list_filters_by_tag(idx):
    idx.add("critical auth bug", tags=["bug", "auth", "critical"])
    idx.add("performance issue", tags=["perf"])
    idx.add("another bug fix", tags=["bug"])
    chunks = idx.list(tag="bug")
    assert len(chunks) == 2
    assert all("bug" in c.tags for c in chunks)


def test_delete_removes_chunk_returns_true(idx):
    chunk_id = idx.add("chunk to be deleted")
    result = idx.delete(chunk_id)
    assert result is True
    assert idx.get(chunk_id) is None


def test_delete_nonexistent_returns_false(idx):
    result = idx.delete("nonexistent-id-xyz")
    assert result is False


def test_count_reflects_adds_and_deletes(idx):
    assert idx.count() == 0
    id1 = idx.add("first")
    assert idx.count() == 1
    idx.add("second")
    assert idx.count() == 2
    idx.delete(id1)
    assert idx.count() == 1


def test_get_returns_chunk(idx):
    chunk_id = idx.add("retrievable content", source="test-src", tags=["t1"])
    chunk = idx.get(chunk_id)
    assert chunk is not None
    assert chunk.id == chunk_id
    assert chunk.content == "retrievable content"
    assert chunk.source == "test-src"
    assert "t1" in chunk.tags


def test_get_nonexistent_returns_none(idx):
    assert idx.get("no-such-id") is None


def test_search_result_has_snippet(idx):
    idx.add("The authentication system uses JWT tokens for session management")
    results = idx.search("authentication JWT")
    assert len(results) >= 1
    assert len(results[0].snippet) > 0


def test_chunk_to_dict(idx):
    chunk_id = idx.add("dict test", source="src", tags=["a", "b"])
    chunk = idx.get(chunk_id)
    assert chunk is not None
    d = chunk.to_dict()
    assert d["id"] == chunk_id
    assert d["content"] == "dict test"
    assert d["source"] == "src"
    assert "a" in d["tags"]
    assert "created_at" in d


def test_context_manager(tmp_path):
    db = tmp_path / "ctx.db"
    with Index(db) as ix:
        ix.add("context manager test")
        assert ix.count() == 1
    # Connection should be closed after __exit__
    assert ix._conn is None


# ── MCP tool tests ─────────────────────────────────────────────────────────────


@pytest.fixture()
def mcp_idx(tmp_path):
    """Wire a temporary Index into the MCP module globals."""
    db = tmp_path / "mcp_test.db"
    test_index = Index(db)
    original = mcp_module._idx
    mcp_module._idx = test_index
    yield test_index
    mcp_module._idx = original
    test_index.close()


def test_mcp_index_add(mcp_idx):
    result = mcp_module.index_add(
        "JWT secret must be rotated monthly", source="security", tags="auth,critical"
    )
    assert "Stored chunk" in result
    assert mcp_idx.count() == 1


def test_mcp_index_add_tags_parsed(mcp_idx):
    mcp_module.index_add("tagged content", tags="a, b, c")
    chunks = mcp_idx.list()
    assert len(chunks) == 1
    assert set(chunks[0].tags) == {"a", "b", "c"}


def test_mcp_index_search_finds_results(mcp_idx):
    mcp_module.index_add("database connection pooling with asyncpg", source="backend")
    result = mcp_module.index_search("database connection")
    assert "database connection" in result.lower() or "asyncpg" in result.lower()
    assert "Found" in result


def test_mcp_index_search_no_results(mcp_idx):
    mcp_module.index_add("Python list comprehension tutorial")
    result = mcp_module.index_search("quantum physics dark matter")
    assert "No results" in result


def test_mcp_index_list_all(mcp_idx):
    mcp_module.index_add("chunk one", source="proj")
    mcp_module.index_add("chunk two", source="proj")
    result = mcp_module.index_list()
    assert "2 chunk" in result


def test_mcp_index_list_by_source(mcp_idx):
    mcp_module.index_add("belongs to alpha", source="alpha")
    mcp_module.index_add("belongs to beta", source="beta")
    result = mcp_module.index_list(source="alpha")
    assert "alpha" in result
    assert "1 chunk" in result


def test_mcp_index_list_empty(mcp_idx):
    result = mcp_module.index_list()
    assert "No chunks" in result


def test_mcp_index_delete_existing(mcp_idx):
    add_result = mcp_module.index_add("to be deleted")
    chunk_id = add_result.split()[-1]
    result = mcp_module.index_delete(chunk_id)
    assert "Deleted" in result
    assert mcp_idx.count() == 0


def test_mcp_index_delete_nonexistent(mcp_idx):
    result = mcp_module.index_delete("no-such-id-xyz")
    assert "not found" in result.lower()


def test_mcp_index_stats_empty(mcp_idx):
    result = mcp_module.index_stats()
    assert "Total chunks: 0" in result


def test_mcp_index_stats_with_data(mcp_idx):
    mcp_module.index_add("first memory chunk")
    mcp_module.index_add("second memory chunk")
    result = mcp_module.index_stats()
    assert "Total chunks: 2" in result
    assert "Recent additions" in result
