"""Tests for the Recorder capture layer."""

from north9.prism.capture import Recorder
from north9.prism.session import Session


class FakeMessages:
    def create(self, **kwargs):
        return {
            "id": "msg_test",
            "content": [{"type": "text", "text": "test response"}],
            "usage": {"input_tokens": 5, "output_tokens": 10},
        }


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()
        self.api_key = "sk-test"


class TestRecorder:
    def test_record_llm_frame(self):
        rec = Recorder()
        rec.record_llm(
            input_params={"model": "claude", "messages": []},
            output={"content": [{"text": "hi"}]},
            elapsed_ms=250,
        )
        assert len(rec.session.frames) == 1
        f = rec.session.frames[0]
        assert f.type == "llm"
        assert f.elapsed_ms == 250

    def test_record_tool_frame(self):
        rec = Recorder()
        rec.record_tool("bash", {"cmd": "ls"}, "file.txt", 30)
        f = rec.session.frames[0]
        assert f.type == "tool"
        assert f.tool == "bash"
        assert f.output == {"result": "file.txt"}

    def test_record_tool_dict_output_not_double_wrapped(self):
        rec = Recorder()
        rec.record_tool("bash", {"cmd": "ls"}, {"stdout": "file.txt", "exit_code": 0}, 30)
        assert rec.session.frames[0].output == {"stdout": "file.txt", "exit_code": 0}

    def test_wrap_anthropic_records_call(self):
        rec = Recorder()
        client = rec.wrap_anthropic(FakeClient())
        resp = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert len(rec.session.frames) == 1
        assert rec.session.frames[0].type == "llm"

    def test_wrap_anthropic_passthrough(self):
        """Non-messages attrs pass through to the real client."""
        rec = Recorder()
        client = rec.wrap_anthropic(FakeClient())
        assert client.api_key == "sk-test"

    def test_frame_ids_increment(self):
        rec = Recorder()
        rec.record_llm({}, {}, 100)
        rec.record_llm({}, {}, 100)
        rec.record_tool("bash", {}, "ok", 10)
        ids = [f.id for f in rec.session.frames]
        assert ids == [0, 1, 2]

    def test_tool_helper(self):
        rec = Recorder()
        result = rec.tool("bash", {"cmd": "ls"}, lambda: "hello")
        assert result == "hello"
        assert rec.session.frames[0].tool == "bash"

    def test_save_and_reload(self, tmp_path):
        rec = Recorder()
        rec.record_llm({"model": "m"}, {"content": "c"}, 100)
        p = str(tmp_path / "test.prism")
        rec.save(p)
        s = Session.load(p)
        assert len(s.frames) == 1

    def test_record_context_manager(self, tmp_path):
        from north9.prism.capture import record

        p = str(tmp_path / "ctx.prism")
        with record(p, metadata={"run": "test"}) as rec:
            client = rec.wrap_anthropic(FakeClient())
            client.messages.create(model="m", messages=[])

        s = Session.load(p)
        assert len(s.frames) == 1
        assert s.metadata["run"] == "test"
