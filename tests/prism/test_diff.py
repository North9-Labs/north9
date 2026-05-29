"""Tests for session diffing."""

from north9.prism.session import Frame, Session
from north9.prism.diff import diff


def _llm(fid, text):
    return Frame(
        id=fid, type="llm", ts=1000.0, elapsed_ms=200,
        input={"messages": [{"role": "user", "content": "q"}]},
        output={"content": [{"text": text}]},
    )


def _tool(fid, result):
    return Frame(
        id=fid, type="tool", ts=1001.0, elapsed_ms=30,
        tool="bash",
        input={"cmd": "ls"},
        output={"result": result},
    )


class TestDiff:
    def test_identical_sessions(self):
        s = Session(frames=[_llm(0, "hello"), _tool(1, "ok")])
        result = diff(s, s)
        assert result.is_identical()

    def test_detects_output_change(self):
        left = Session(frames=[_llm(0, "hello")])
        right = Session(frames=[_llm(0, "world")])
        result = diff(left, right)
        assert not result.is_identical()
        assert result.fork_point == 0
        assert any(fd.field == "output" for fd in result.frame_diffs)

    def test_detects_extra_frame_in_right(self):
        left = Session(frames=[_llm(0, "a")])
        right = Session(frames=[_llm(0, "a"), _llm(1, "b")])
        result = diff(left, right)
        assert not result.is_identical()
        assert len(result.right_only) == 1
        assert result.right_only[0].id == 1

    def test_detects_extra_frame_in_left(self):
        left = Session(frames=[_llm(0, "a"), _llm(1, "b")])
        right = Session(frames=[_llm(0, "a")])
        result = diff(left, right)
        assert not result.is_identical()
        assert len(result.left_only) == 1

    def test_summary_contains_fork_point(self):
        left = Session(frames=[_llm(0, "a"), _llm(1, "different")])
        right = Session(frames=[_llm(0, "a"), _llm(1, "changed")])
        result = diff(left, right)
        summary = result.summary()
        assert "frame 1" in summary

    def test_summary_identical(self):
        s = Session(frames=[_llm(0, "same")])
        result = diff(s, s)
        assert "identical" in result.summary().lower()
