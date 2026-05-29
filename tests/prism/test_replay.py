"""Tests for the replay engine."""

import pytest

from north9.prism.session import Frame, Session
from north9.prism.replay import Replayer, ReplayMode, ReplayError


def _llm_frame(fid: int, content: str, tokens_in=10, tokens_out=20) -> Frame:
    return Frame(
        id=fid,
        type="llm",
        ts=1000.0,
        elapsed_ms=300,
        input={"messages": [{"role": "user", "content": "q"}]},
        output={
            "content": [{"type": "text", "text": content}],
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        },
    )


def _tool_frame(fid: int, tool: str, result: str) -> Frame:
    return Frame(
        id=fid,
        type="tool",
        ts=1001.0,
        elapsed_ms=50,
        tool=tool,
        input={"cmd": "ls"},
        output={"result": result},
    )


class TestReplayer:
    def test_next_llm_returns_recorded_output(self):
        s = Session(frames=[_llm_frame(0, "hello"), _llm_frame(1, "world")])
        r = Replayer(s)
        out = r.next_llm()
        assert out["content"][0]["text"] == "hello"
        out2 = r.next_llm()
        assert out2["content"][0]["text"] == "world"

    def test_next_llm_exhausted_raises(self):
        s = Session(frames=[_llm_frame(0, "only one")])
        r = Replayer(s)
        r.next_llm()
        with pytest.raises(ReplayError, match="Replay exhausted"):
            r.next_llm()

    def test_next_tool_returns_recorded_output(self):
        s = Session(frames=[_tool_frame(0, "bash", "file1.txt\nfile2.txt")])
        r = Replayer(s)
        result = r.next_tool("bash")
        assert result == "file1.txt\nfile2.txt"

    def test_next_tool_unknown_raises(self):
        s = Session(frames=[_tool_frame(0, "bash", "ok")])
        r = Replayer(s)
        with pytest.raises(ReplayError, match="no recorded frame"):
            r.next_tool("python")

    def test_stats(self):
        s = Session(frames=[_llm_frame(0, "a"), _tool_frame(1, "bash", "b")])
        r = Replayer(s)
        r.next_llm()
        r.next_tool("bash")
        assert r.stats["llm_frames_served"] == 1
        assert r.stats["tool_frames_served"] == 1

    def test_tool_in_live_mode_calls_fn(self):
        s = Session(frames=[_tool_frame(0, "bash", "recorded")])
        r = Replayer(s, mode=ReplayMode.LIVE)
        called = []
        result = r.tool("bash", {"cmd": "ls"}, fn=lambda: called.append(True) or "live")
        assert result == "live"
        assert called

    def test_wrap_anthropic_returns_dict_response(self):
        """Wrapped client's messages.create returns recorded output."""
        s = Session(frames=[_llm_frame(0, "recorded response")])
        r = Replayer(s)

        class FakeMessages:
            def create(self, **kwargs):
                return {}

        class FakeClient:
            def __init__(self):
                self.messages = FakeMessages()

        wrapped = r.wrap_anthropic(FakeClient())
        resp = wrapped.messages.create(model="test", messages=[])
        assert resp.content[0].text == "recorded response"


class TestDictResponse:
    def test_attribute_access(self):
        from north9.prism.replay import _DictResponse

        r = _DictResponse({"content": [{"text": "hi"}], "usage": {"input_tokens": 5}})
        assert r.content[0].text == "hi"
        assert r.usage.input_tokens == 5

    def test_missing_attribute_raises(self):
        from north9.prism.replay import _DictResponse

        r = _DictResponse({"x": 1})
        with pytest.raises(AttributeError):
            _ = r.nonexistent

    def test_model_dump(self):
        from north9.prism.replay import _DictResponse

        data = {"a": 1, "b": 2}
        r = _DictResponse(data)
        assert r.model_dump() == data
