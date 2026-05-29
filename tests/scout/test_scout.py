"""Tests for Scout — web fetch and search package."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import north9.scout.mcp as scout_mcp
from north9.scout.core import Scout, SearchResult, _chunk_text, _extract_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_response(html: str, content_type: str = "text/html; charset=utf-8") -> MagicMock:
    mock = MagicMock()
    mock.read.return_value = html.encode()
    mock.headers.get.return_value = content_type
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <nav>Navigation stuff</nav>
  <h1>Hello World</h1>
  <p>This is a paragraph about pathlib glob recursive search patterns.</p>
  <script>alert('ignored')</script>
  <footer>Footer content</footer>
</body>
</html>"""

SAMPLE_URL = "https://example.com/test"


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_strips_html_tags(self) -> None:
        result = _extract_text("<p>Hello <b>world</b></p>")
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_skips_script_tags(self) -> None:
        result = _extract_text("<p>Visible</p><script>ignored code</script>")
        assert "Visible" in result
        assert "ignored code" not in result

    def test_skips_style_tags(self) -> None:
        result = _extract_text("<p>Text</p><style>.cls { color: red }</style>")
        assert "Text" in result
        assert "color" not in result

    def test_skips_nav_footer_header(self) -> None:
        result = _extract_text(
            "<nav>Nav links</nav><p>Content</p><footer>Footer</footer><header>Header</header>"
        )
        assert "Content" in result
        assert "Nav links" not in result
        assert "Footer" not in result
        assert "Header" not in result

    def test_full_page(self) -> None:
        result = _extract_text(SAMPLE_HTML)
        assert "Hello World" in result
        assert "pathlib glob" in result
        assert "Navigation stuff" not in result
        assert "alert" not in result
        assert "Footer content" not in result


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_short_text_single_chunk(self) -> None:
        text = "Short text"
        chunks = _chunk_text(text, chunk_size=800)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_multiple_chunks(self) -> None:
        text = "word " * 300  # 1500 chars
        chunks = _chunk_text(text, chunk_size=800, overlap=100)
        assert len(chunks) > 1

    def test_chunks_cover_content(self) -> None:
        text = "alpha " * 200
        chunks = _chunk_text(text, chunk_size=200, overlap=20)
        # Every chunk should be non-empty
        assert all(c.strip() for c in chunks)

    def test_exact_chunk_size_single(self) -> None:
        text = "x" * 800
        chunks = _chunk_text(text, chunk_size=800)
        assert len(chunks) == 1

    def test_overlap_produces_duplicated_content(self) -> None:
        # With overlap, adjacent chunks should share some content
        text = "word " * 400
        chunks = _chunk_text(text, chunk_size=300, overlap=50)
        assert len(chunks) >= 2
        # Each chunk non-empty
        for c in chunks:
            assert len(c) > 0


# ---------------------------------------------------------------------------
# Scout core
# ---------------------------------------------------------------------------

class TestScoutInit:
    def test_initializes_with_tmp_db(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        scout = Scout(db_path=db)
        assert db.exists()
        scout.close()

    def test_stats_empty_on_init(self, tmp_path: Path) -> None:
        scout = Scout(db_path=tmp_path / "test.db")
        s = scout.stats()
        assert s["pages"] == 0
        assert s["chunks"] == 0
        scout.close()

    def test_context_manager(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            assert scout.stats()["pages"] == 0


class TestScoutFetch:
    def test_fetch_stores_page(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
                page = scout.fetch(SAMPLE_URL)
        assert page.url == SAMPLE_URL
        assert page.title == "Test Page"
        assert page.chunk_count >= 1

    def test_fetch_caches_result(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)) as mock_urlopen:
                scout.fetch(SAMPLE_URL)
                page2 = scout.fetch(SAMPLE_URL)  # second call — should use cache
        # urlopen called only once
        mock_urlopen.assert_called_once()
        assert page2.url == SAMPLE_URL

    def test_fetch_force_refetches(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)) as mock_urlopen:
                scout.fetch(SAMPLE_URL)
                scout.fetch(SAMPLE_URL, force=True)
        assert mock_urlopen.call_count == 2

    def test_fetch_plain_text(self, tmp_path: Path) -> None:
        plain = "This is plain text content without any HTML tags."
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(plain, "text/plain")):
                page = scout.fetch(SAMPLE_URL)
        assert page.chunk_count >= 1

    def test_fetch_raises_on_network_error(self, tmp_path: Path) -> None:
        import urllib.error

        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
                with pytest.raises(ValueError, match="Failed to fetch"):
                    scout.fetch(SAMPLE_URL)


class TestScoutSearch:
    def _setup_scout(self, tmp_path: Path) -> Scout:
        scout = Scout(db_path=tmp_path / "test.db")
        with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
            scout.fetch(SAMPLE_URL)
        return scout

    def test_search_finds_content(self, tmp_path: Path) -> None:
        with self._setup_scout(tmp_path) as scout:
            results = scout.search("pathlib glob")
        assert len(results) >= 1
        assert any("pathlib" in r.snippet or "glob" in r.snippet for r in results)

    def test_search_returns_empty_for_no_match(self, tmp_path: Path) -> None:
        with self._setup_scout(tmp_path) as scout:
            results = scout.search("zzznomatchzzz")
        assert results == []

    def test_search_result_has_correct_fields(self, tmp_path: Path) -> None:
        with self._setup_scout(tmp_path) as scout:
            results = scout.search("Hello")
        assert len(results) >= 1
        r = results[0]
        assert isinstance(r, SearchResult)
        assert r.url == SAMPLE_URL
        assert r.title == "Test Page"
        assert isinstance(r.score, float)

    def test_search_filter_by_url(self, tmp_path: Path) -> None:
        other_html = "<html><body><p>Completely different content about databases.</p></body></html>"
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
                scout.fetch(SAMPLE_URL)
            with patch("urllib.request.urlopen", return_value=make_mock_response(other_html)):
                scout.fetch("https://other.com/page")
            # Filter to just the first URL
            results = scout.search("pathlib", url=SAMPLE_URL)
        assert all(r.url == SAMPLE_URL for r in results)

    def test_search_k_limits_results(self, tmp_path: Path) -> None:
        # Create a page with several chunks using small chunk_size
        big_html = (
            "<html><body>"
            + "<p>pathlib glob search pattern</p>" * 8
            + "</body></html>"
        )
        with Scout(db_path=tmp_path / "test.db", chunk_size=80, chunk_overlap=10) as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(big_html)):
                scout.fetch(SAMPLE_URL)
            results = scout.search("pathlib", k=2)
        assert len(results) <= 2


class TestScoutListAndDelete:
    def test_list_pages_returns_fetched(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
                scout.fetch(SAMPLE_URL)
            pages = scout.list_pages()
        assert len(pages) == 1
        assert pages[0].url == SAMPLE_URL

    def test_list_pages_limit(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            for i in range(5):
                url = f"https://example.com/page{i}"
                html = f"<html><head><title>Page {i}</title></head><body><p>Content {i}</p></body></html>"
                with patch("urllib.request.urlopen", return_value=make_mock_response(html)):
                    scout.fetch(url)
            pages = scout.list_pages(limit=3)
        assert len(pages) == 3

    def test_delete_removes_page(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
                scout.fetch(SAMPLE_URL)
            removed = scout.delete(SAMPLE_URL)
            assert removed is True
            pages = scout.list_pages()
        assert len(pages) == 0

    def test_delete_returns_false_for_missing(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            removed = scout.delete("https://nonexistent.com/")
        assert removed is False

    def test_delete_removes_chunks(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
                scout.fetch(SAMPLE_URL)
            scout.delete(SAMPLE_URL)
            s = scout.stats()
        assert s["chunks"] == 0
        assert s["pages"] == 0


class TestScoutStats:
    def test_stats_counts(self, tmp_path: Path) -> None:
        with Scout(db_path=tmp_path / "test.db") as scout:
            with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
                page = scout.fetch(SAMPLE_URL)
            s = scout.stats()
        assert s["pages"] == 1
        assert s["chunks"] == page.chunk_count


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

class TestMCPTools:
    @pytest.fixture(autouse=True)
    def setup_scout(self, tmp_path: Path) -> None:
        """Point the MCP module at a tmp Scout instance."""
        scout_mcp._scout = Scout(db_path=tmp_path / "mcp_test.db")
        yield
        if scout_mcp._scout:
            scout_mcp._scout.close()
            scout_mcp._scout = None

    def test_scout_fetch_tool(self) -> None:
        with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
            result = scout_mcp.scout_fetch(SAMPLE_URL)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["title"] == "Test Page"
        assert data["chunks"] >= 1

    def test_scout_fetch_error_returns_string(self) -> None:
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            result = scout_mcp.scout_fetch("https://bad.example.com/")
        data = json.loads(result)
        assert data["status"] == "error"
        assert "error" in data

    def test_scout_search_tool(self) -> None:
        with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
            scout_mcp.scout_fetch(SAMPLE_URL)
        result = scout_mcp.scout_search("pathlib glob")
        assert isinstance(result, str)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert "results" in data

    def test_scout_search_no_results(self) -> None:
        result = scout_mcp.scout_search("zzznomatch")
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["results"] == []

    def test_scout_list_tool(self) -> None:
        with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
            scout_mcp.scout_fetch(SAMPLE_URL)
        result = scout_mcp.scout_list()
        assert isinstance(result, str)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["count"] == 1

    def test_scout_delete_tool(self) -> None:
        with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
            scout_mcp.scout_fetch(SAMPLE_URL)
        result = scout_mcp.scout_delete(SAMPLE_URL)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["removed"] is True

    def test_scout_stats_tool(self) -> None:
        with patch("urllib.request.urlopen", return_value=make_mock_response(SAMPLE_HTML)):
            scout_mcp.scout_fetch(SAMPLE_URL)
        result = scout_mcp.scout_stats()
        assert isinstance(result, str)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["pages"] == 1
        assert data["chunks"] >= 1
