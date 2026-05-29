"""Tests for session storage and fork logic."""

import json
import tempfile
from pathlib import Path

import pytest

from north9.prism.session import Frame, Session


def make_session(n_llm=2, n_tool=1) -> Session:
    s = Session(metadata={"test": True})
    fid = 0
    for i in range(n_llm):
        s.frames.append(
            Frame(
                id=fid,
                type="llm",
                ts=1000.0 + fid,
                elapsed_ms=500 + i * 100,
                input={"model": "claude-3-5-sonnet-20241022", "messages": [{"role": "user", "content": f"hello {i}"}]},
                output={"id": f"msg_{i}", "content": [{"type": "text", "text": f"response {i}"}], "usage": {"input_tokens": 10, "output_tokens": 20}},
            )
        )
        fid += 1
        if i < n_tool:
            s.frames.append(
                Frame(
                    id=fid,
                    type="tool",
                    ts=1001.0 + fid,
                    elapsed_ms=50,
                    tool="bash",
                    input={"command": f"ls -la {i}"},
                    output={"result": f"output {i}"},
                )
            )
            fid += 1
    return s


class TestSessionPersistence:
    def test_roundtrip(self, tmp_path):
        s = make_session()
        p = tmp_path / "test.prism"
        s.save(p)
        loaded = Session.load(p)
        assert loaded.session_id == s.session_id
        assert len(loaded.frames) == len(s.frames)
        assert loaded.frames[0].input == s.frames[0].input
        assert loaded.frames[0].output == s.frames[0].output

    def test_metadata_roundtrip(self, tmp_path):
        s = Session(metadata={"model": "claude", "task": "refactor"})
        p = tmp_path / "meta.prism"
        s.save(p)
        loaded = Session.load(p)
        assert loaded.metadata["model"] == "claude"
        assert loaded.metadata["task"] == "refactor"

    def test_empty_session(self, tmp_path):
        s = Session()
        p = tmp_path / "empty.prism"
        s.save(p)
        loaded = Session.load(p)
        assert len(loaded.frames) == 0

    def test_invalid_file_raises(self, tmp_path):
        p = tmp_path / "bad.prism"
        p.write_text('{"not": "prism"}\n')
        with pytest.raises(ValueError, match="not a Prism session file"):
            Session.load(p)


class TestSessionProperties:
    def test_llm_frames(self):
        s = make_session(n_llm=3, n_tool=2)
        assert len(s.llm_frames) == 3
        assert all(f.type == "llm" for f in s.llm_frames)

    def test_tool_frames(self):
        s = make_session(n_llm=2, n_tool=2)
        assert len(s.tool_frames) == 2
        assert all(f.type == "tool" for f in s.tool_frames)

    def test_total_tokens(self):
        s = make_session(n_llm=2, n_tool=0)
        # Each LLM frame has 10 in + 20 out = 30 each; 2 frames = 60
        assert s.total_tokens == 60

    def test_summary_contains_key_info(self):
        s = make_session()
        summary = s.summary()
        assert "frames" in summary
        assert "tokens" in summary


class TestFork:
    def test_fork_creates_correct_prefix(self):
        s = make_session(n_llm=3, n_tool=0)
        fork = s.fork(at_frame=2)
        assert len(fork.prefix_frames) == 2
        assert fork.fork_point == 2

    def test_fork_applies_patch(self):
        s = make_session(n_llm=2, n_tool=0)
        fork = s.fork(at_frame=1, patch={"model": "claude-3-haiku-20240307"})
        assert fork.pivot_input["model"] == "claude-3-haiku-20240307"
        # Original messages still present (merged)
        assert "messages" in fork.pivot_input

    def test_fork_at_first_frame(self):
        s = make_session(n_llm=2, n_tool=0)
        fork = s.fork(at_frame=0, patch={"model": "gpt-4o"})
        assert len(fork.prefix_frames) == 0
        assert fork.fork_point == 0

    def test_fork_out_of_range_raises(self):
        s = make_session(n_llm=1, n_tool=0)
        with pytest.raises(IndexError):
            s.fork(at_frame=99)

    def test_fork_description(self):
        s = make_session()
        fork = s.fork(at_frame=1)
        desc = fork.description()
        assert "Fork" in desc
        assert "frame 1" in desc
