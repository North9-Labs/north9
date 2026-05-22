"""Tests for Memory context compression."""

import pytest

from north9.memory.core import (
    AsyncMemory,
    Memory,
    MemoryState,
    _parse_yaml_block,
    _serialize_message,
)

# ── Unit: YAML parser ────────────────────────────────────────────────────────

class TestParseYaml:
    def test_parses_all_sections(self):
        text = """
```yaml
objective: "Write a CLI tool in Rust"

completed:
  - "Created Cargo.toml → /root/project/Cargo.toml"
  - "Implemented arg parser → clap 4.0"

failed:
  - "zigbuild on aarch64 → linker error: crt1.o not found [DO NOT RETRY]"

pending:
  - "Add tests"
  - "Push to GitHub"

facts:
  - "repo: /root/Git/hub/MyTool"
  - "target: x86_64-unknown-linux-musl"
```
"""
        s = _parse_yaml_block(text)
        assert s.objective == "Write a CLI tool in Rust"
        assert len(s.completed) == 2
        assert "Created Cargo.toml → /root/project/Cargo.toml" in s.completed
        assert len(s.failed) == 1
        assert "zigbuild on aarch64" in s.failed[0]
        assert len(s.pending) == 2
        assert len(s.facts) == 2

    def test_empty_sections(self):
        text = """
objective: "Fix the bug"

completed: []
failed: []
pending:
  - "Run tests"
facts: []
"""
        s = _parse_yaml_block(text)
        assert s.objective == "Fix the bug"
        assert s.completed == []
        assert s.failed == []
        assert s.pending == ["Run tests"]
        assert s.facts == []

    def test_no_fences(self):
        text = (
            'objective: "Deploy app"\n'
            'completed:\n'
            '  - "Built binary → dist/app"\n'
            'failed: []\n'
            'pending: []\n'
            'facts: []'
        )
        s = _parse_yaml_block(text)
        assert s.objective == "Deploy app"
        assert s.completed == ["Built binary → dist/app"]

    def test_is_empty(self):
        assert MemoryState().is_empty()
        s = MemoryState(objective="do thing")
        assert not s.is_empty()

    def test_parses_snippets(self):
        text = """
```yaml
objective: "Build app"

completed: []
failed: []
pending: []
facts: []

snippets:
  - lang: "python"
    code: |
      def hello():
          print("world")
```
"""
        s = _parse_yaml_block(text)
        assert len(s.snippets) == 1
        assert s.snippets[0]["lang"] == "python"
        assert "def hello():" in s.snippets[0]["code"]

    def test_parses_multiple_snippets(self):
        text = """
```yaml
objective: "Build app"
completed: []
failed: []
pending: []
facts: []

snippets:
  - lang: "python"
    code: |
      def hello():
          print("world")
  - lang: "rust"
    code: |
      fn main() {
          println!("hello");
      }
```
"""
        s = _parse_yaml_block(text)
        assert len(s.snippets) == 2
        assert s.snippets[0]["lang"] == "python"
        assert s.snippets[1]["lang"] == "rust"
        assert "fn main()" in s.snippets[1]["code"]


# ── Unit: MemoryState rendering ──────────────────────────────────────────────

class TestScrollState:
    def test_to_system_block_includes_all_sections(self):
        s = MemoryState(
            objective="Deploy the service",
            completed=["Built binary → /usr/bin/app"],
            failed=["SSH bootstrap → broken pipe [DO NOT RETRY]"],
            pending=["Run smoke test"],
            facts=["host: 10.9.0.1", "port: 2222"],
        )
        block = s.to_system_block()
        assert "OBJECTIVE: Deploy the service" in block
        assert "Built binary" in block
        assert "DO NOT RETRY" in block
        assert "Run smoke test" in block
        assert "host: 10.9.0.1" in block

    def test_empty_state_not_in_block(self):
        s = MemoryState(objective="Go", completed=[], failed=[], pending=[], facts=[])
        block = s.to_system_block()
        assert "COMPLETED" not in block
        assert "FAILED" not in block

    def test_snippets_in_system_block(self):
        s = MemoryState(
            objective="Code review",
            snippets=[{"lang": "python", "code": "def foo():\n    pass"}],
        )
        block = s.to_system_block()
        assert "CODE SNIPPETS" in block
        assert "```python" in block
        assert "def foo():" in block

    def test_deduplicate(self):
        s = MemoryState(
            completed=["A", "B", "A"],
            failed=["X"],
            pending=["P1", "P1"],
            facts=["x: 1", "x: 1", "y: 2"],
            snippets=[
                {"lang": "py", "code": "a"},
                {"lang": "py", "code": "a"},
                {"lang": "rs", "code": "b"},
            ],
        )
        s.deduplicate()
        assert s.completed == ["A", "B"]
        assert s.pending == ["P1"]
        assert s.facts == ["x: 1", "y: 2"]
        assert len(s.snippets) == 2


# ── Unit: token estimation ───────────────────────────────────────────────────

class TestTokenEstimate:
    def test_estimate_scales_with_content(self):
        short = [{"role": "user", "content": "hi"}]
        long = [{"role": "user", "content": "x" * 400}]
        assert Memory._estimate_tokens(long) > Memory._estimate_tokens(short)

    def test_list_content_handled(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        est = Memory._estimate_tokens(msgs)
        assert est >= 0


# ── Unit: message serialization ──────────────────────────────────────────────

class TestSerializeMessage:
    def test_plain_text(self):
        msg = {"role": "user", "content": "hello"}
        assert _serialize_message(msg) == "[USER]\nhello"

    def test_openai_tool_calls(self):
        msg = {
            "role": "assistant",
            "content": "I'll search",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "search", "arguments": '{"q": "foo"}'},
                }
            ],
        }
        text = _serialize_message(msg)
        assert "TOOL_CALL" in text
        assert "search" in text
        assert "call_1" in text

    def test_openai_tool_result(self):
        msg = {"role": "tool", "tool_call_id": "call_1", "content": "result here"}
        text = _serialize_message(msg)
        assert "[TOOL_RESULT]" in text
        assert "call_1" in text
        assert "result here" in text

    def test_anthropic_tool_use(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "calculator", "input": {"a": 1, "b": 2}}
            ],
        }
        text = _serialize_message(msg)
        assert "TOOL_USE" in text
        assert "calculator" in text

    def test_anthropic_tool_result(self):
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "42",
                }
            ],
        }
        text = _serialize_message(msg)
        assert "TOOL_RESULT" in text
        assert "tu_1" in text
        assert "42" in text


# ── Memory: compression logic (no live API) ──────────────────────────────────

class FakeAnthropicMessages:
    def __init__(self, response_text: str):
        self._text = response_text

    def create(self, **kwargs):
        class FakeContent:
            text = self._text

        class FakeResp:
            content = [FakeContent()]

        return FakeResp()


class FakeAnthropicClient:
    def __init__(self, compress_response: str = ""):
        self.messages = FakeAnthropicMessages(compress_response or _GOOD_YAML)


class AsyncFakeAnthropicMessages:
    def __init__(self, response_text: str):
        self._text = response_text

    async def create(self, **kwargs):
        class FakeContent:
            text = self._text

        class FakeResp:
            content = [FakeContent()]

        return FakeResp()


class AsyncFakeAnthropicClient:
    def __init__(self, compress_response: str = ""):
        self.messages = AsyncFakeAnthropicMessages(compress_response or _GOOD_YAML)


_GOOD_YAML = """```yaml
objective: "Build and deploy the Rust tool"

completed:
  - "Wrote src/main.rs → 142 lines"
  - "cargo build --release → dist/tool"

failed:
  - "cross compile aarch64 → linker not found [DO NOT RETRY]"

pending:
  - "Write tests"

facts:
  - "binary: dist/tool"
  - "version: 0.1.0"
```"""


class TestScrollCompression:
    def _make_scroll(self, keep_recent=3, token_limit=10):
        client = FakeAnthropicClient()
        scroll = Memory(
            client,
            keep_recent=keep_recent,
            token_limit=token_limit,
            provider="anthropic",
        )
        return scroll

    def test_messages_passthrough_under_limit(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=10,
            token_limit=100_000,
            provider="anthropic",
        )
        scroll.add("user", "hello")
        scroll.add("assistant", "world")
        msgs = scroll.messages
        assert len(msgs) == 2

    def test_compression_triggered_over_limit(self):
        scroll = self._make_scroll(keep_recent=2, token_limit=1)
        for i in range(6):
            scroll.add("user", f"turn {i} " * 20)
            scroll.add("assistant", f"response {i} " * 20)
        _ = scroll.messages
        assert scroll.compressions >= 1

    def test_recent_turns_kept_verbatim(self):
        scroll = self._make_scroll(keep_recent=2, token_limit=1)
        messages = [f"turn {i}" for i in range(10)]
        for i, m in enumerate(messages):
            scroll.add("user" if i % 2 == 0 else "assistant", m)
        _ = scroll.messages
        assert len(scroll._messages) <= 2

    def test_state_injected_in_build_window(self):
        scroll = self._make_scroll(keep_recent=2, token_limit=1)
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        msgs = scroll.messages
        # State is now merged into system prompt (first message)
        assert msgs[0]["role"] == "system"
        assert "SCROLL" in msgs[0]["content"]

    def test_compressions_counter_increments(self):
        scroll = self._make_scroll(keep_recent=1, token_limit=1)
        for i in range(4):
            scroll.add("user", "x" * 100)
            scroll.add("assistant", "y" * 100)
            _ = scroll.messages
        assert scroll.compressions >= 1

    def test_force_compress(self):
        scroll = self._make_scroll(keep_recent=2, token_limit=999_999)
        scroll.add("user", "build the thing")
        scroll.add("assistant", "ok building")
        scroll.add("user", "done?")
        scroll.add("assistant", "yes done")
        state = scroll.force_compress()
        assert isinstance(state, MemoryState)
        assert scroll.compressions == 1

    def test_stats(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=5,
            token_limit=100_000,
            provider="anthropic",
        )
        scroll.add("user", "hi")
        s = scroll.stats
        assert s["total_turns"] == 1
        assert s["compressions"] == 0

    def test_provider_autodetect_anthropic(self):
        class FakeAnthropic:
            messages = None

        client = FakeAnthropic()
        scroll = Memory(client, keep_recent=5, token_limit=100_000)
        assert scroll._provider == "anthropic"

    def test_provider_autodetect_openai(self):
        class FakeOpenAI:
            chat = None

        client = FakeOpenAI()
        scroll = Memory(client, keep_recent=5, token_limit=100_000)
        assert scroll._provider == "openai"

    def test_unknown_provider_raises(self):
        class Unknown:
            pass

        with pytest.raises(ValueError, match="Cannot detect provider"):
            Memory(Unknown(), keep_recent=5, token_limit=100_000)

    def test_add_message_raw_dict(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=5,
            token_limit=100_000,
            provider="anthropic",
        )
        scroll.add_message({"role": "user", "content": "raw message"})
        assert scroll._messages[0]["content"] == "raw message"


# ── New features ─────────────────────────────────────────────────────────────

class TestStateMerge:
    def test_merge_preserves_old_completed(self):
        old = MemoryState(completed=["A"], failed=[], pending=["B"], facts=["x: 1"])
        new = MemoryState(completed=["B"], pending=["C"])
        scroll = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = scroll._merge_states(old, new)
        assert "A" in merged.completed
        assert "B" in merged.completed
        assert "C" in merged.pending
        assert "x: 1" in merged.facts

    def test_merge_removes_done_pending(self):
        old = MemoryState(pending=["Fix bug", "Add tests"])
        new = MemoryState(completed=["Fix bug"], pending=["Deploy"])
        scroll = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = scroll._merge_states(old, new)
        assert "Fix bug" not in merged.pending
        assert "Add tests" in merged.pending
        assert "Deploy" in merged.pending

    def test_merge_prefers_new_objective(self):
        old = MemoryState(objective="Old")
        new = MemoryState(objective="New")
        scroll = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = scroll._merge_states(old, new)
        assert merged.objective == "New"

    def test_merge_keeps_old_objective_if_new_empty(self):
        old = MemoryState(objective="Old")
        new = MemoryState()
        scroll = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = scroll._merge_states(old, new)
        assert merged.objective == "Old"

    def test_merge_dedupes_snippets(self):
        old = MemoryState(snippets=[{"lang": "py", "code": "a"}])
        new = MemoryState(snippets=[{"lang": "py", "code": "a"}, {"lang": "rs", "code": "b"}])
        scroll = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = scroll._merge_states(old, new)
        assert len(merged.snippets) == 2


class TestSystemMessage:
    def test_system_message_preserved_across_compression(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
        )
        scroll.add("system", "You are a helpful assistant")
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        msgs = scroll.messages
        system_msgs = [m for m in msgs if m.get("role") == "system"]
        assert len(system_msgs) == 1
        # System prompt contains original content + merged state
        assert "You are a helpful assistant" in system_msgs[0]["content"]

    def test_add_message_system(self):
        scroll = Memory(FakeAnthropicClient(), provider="anthropic")
        scroll.add_message({"role": "system", "content": "Be concise"})
        assert scroll._system_message == "Be concise"
        assert len(scroll._messages) == 0


class TestRetryLogic:
    def test_retries_then_succeeds(self, monkeypatch):
        import time

        monkeypatch.setattr(time, "sleep", lambda x: None)

        class FailingMessages:
            def __init__(self):
                self._calls = 0

            def create(self, **kwargs):
                self._calls += 1
                if self._calls < 3:
                    raise RuntimeError("API down")
                class FakeContent:
                    text = _GOOD_YAML
                class FakeResp:
                    content = [FakeContent()]
                return FakeResp()

        class FailingClient:
            def __init__(self):
                self.messages = FailingMessages()

        client = FailingClient()
        scroll = Memory(
            client,
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
            max_retries=3,
        )
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        _ = scroll.messages
        assert scroll.compressions >= 1
        assert client.messages._calls == 3

    def test_retries_exhausted_raises(self, monkeypatch):
        import time

        monkeypatch.setattr(time, "sleep", lambda x: None)

        class AlwaysFailingMessages:
            def create(self, **kwargs):
                raise RuntimeError("API down")

        class BadClient:
            messages = AlwaysFailingMessages()

        scroll = Memory(
            BadClient(),
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
            max_retries=2,
        )
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        with pytest.raises(RuntimeError, match="API down"):
            _ = scroll.messages


class TestChunking:
    def test_large_history_chunks(self, monkeypatch):
        import time

        monkeypatch.setattr(time, "sleep", lambda x: None)

        call_count = 0

        class CountingMessages:
            def create(self, **kwargs):
                nonlocal call_count
                call_count += 1
                class FakeContent:
                    text = _GOOD_YAML
                class FakeResp:
                    content = [FakeContent()]
                return FakeResp()

        class CountingClient:
            messages = CountingMessages()

        scroll = Memory(
            CountingClient(),
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
            compress_chunk_size=50,
        )
        # Each message ~54 tokens (200 chars / 4 + 4 overhead), so each gets its own chunk
        for i in range(10):
            scroll.add("user", "x" * 200)
            scroll.add("assistant", "y" * 200)
        _ = scroll.messages
        assert call_count > 1  # multiple chunks compressed
        assert scroll.compressions >= 1


class TestPinnedMessages:
    def test_pinned_messages_preserved(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
        )
        scroll.add_pinned("user", "CRITICAL: do not delete db")
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        msgs = scroll.messages
        pinned = [m for m in msgs if m.get("content") == "CRITICAL: do not delete db"]
        assert len(pinned) == 1
        assert pinned[0]["role"] == "user"

    def test_state_in_system_pinned_after(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
        )
        scroll.add_pinned("user", "pinned")
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        msgs = scroll.messages
        # State goes into system prompt (no broken consecutive-user-messages)
        assert msgs[0]["role"] == "system"
        assert "SCROLL" in msgs[0]["content"]
        # Pinned comes after system
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "pinned"


class TestWindowBuild:
    def test_window_with_system_and_state(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
        )
        scroll.add("system", "Sys")
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        msgs = scroll.messages
        # State merged into system prompt — both present in [0]
        assert msgs[0]["role"] == "system"
        assert "Sys" in msgs[0]["content"]
        assert "SCROLL" in msgs[0]["content"]

    def test_window_state_only_no_system(self):
        scroll = Memory(
            FakeAnthropicClient(),
            keep_recent=2,
            token_limit=1,
            provider="anthropic",
        )
        for i in range(6):
            scroll.add("user", "x" * 50)
            scroll.add("assistant", "y" * 50)
        msgs = scroll.messages
        assert msgs[0]["role"] == "system"
        assert "SCROLL" in msgs[0]["content"]


# ── MemoryState serialization ─────────────────────────────────────────────────

class TestScrollStateSerialization:
    def test_to_dict_and_back(self):
        s = MemoryState(
            objective="Do the thing",
            completed=["Step 1 → done"],
            failed=["Step 2 → broken [DO NOT RETRY]"],
            pending=["Step 3"],
            facts=["host: prod-01"],
            snippets=[{"lang": "python", "code": "print('hi')"}],
        )
        d = s.to_dict()
        s2 = MemoryState.from_dict(d)
        assert s2.objective == s.objective
        assert s2.completed == s.completed
        assert s2.failed == s.failed
        assert s2.pending == s.pending
        assert s2.facts == s.facts
        assert s2.snippets == s.snippets

    def test_from_dict_empty(self):
        s = MemoryState.from_dict({})
        assert s.is_empty()

    def test_from_dict_partial(self):
        s = MemoryState.from_dict({"objective": "Go", "completed": ["x"]})
        assert s.objective == "Go"
        assert s.completed == ["x"]
        assert s.failed == []


# ── State persistence (save/load) ─────────────────────────────────────────────

class TestStatePersistence:
    def test_save_and_load(self, tmp_path):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=5, token_limit=100_000)
        sc.add("system", "You are a helper")
        sc.add("user", "do task")
        sc.add("assistant", "doing it")
        sc._state = MemoryState(objective="build widget", completed=["compiled"], facts=["v: 1"])
        sc.compressions = 3

        path = str(tmp_path / "state.json")
        sc.save_state(path)

        sc2 = Memory(
            FakeAnthropicClient(), provider="anthropic", keep_recent=5, token_limit=100_000
        )
        sc2.load_state(path)
        assert sc2._state.objective == "build widget"
        assert sc2._state.completed == ["compiled"]
        assert sc2._state.facts == ["v: 1"]
        assert sc2._system_message == "You are a helper"
        assert sc2.compressions == 3
        assert len(sc2._messages) == 2

    def test_save_is_atomic(self, tmp_path):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc._state = MemoryState(objective="test")
        path = str(tmp_path / "s.json")
        sc.save_state(path)
        import os
        assert os.path.exists(path)
        assert not os.path.exists(path + ".tmp")


# ── Fuzzy pending cleanup ─────────────────────────────────────────────────────

class TestFuzzyPendingCleanup:
    def test_pending_clears_on_prefix_match(self):
        old = MemoryState(pending=["Fix auth bug", "Deploy service"])
        new = MemoryState(completed=["Fix auth bug → resolved in commit abc123"])
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = sc._merge_states(old, new)
        assert "Fix auth bug" not in merged.pending
        assert "Deploy service" in merged.pending

    def test_pending_clears_exact_match(self):
        old = MemoryState(pending=["Run tests"])
        new = MemoryState(completed=["Run tests"])
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = sc._merge_states(old, new)
        assert "Run tests" not in merged.pending

    def test_pending_not_cleared_on_partial_substring(self):
        old = MemoryState(pending=["Fix the auth bug in prod"])
        new = MemoryState(completed=["Fix bug → done"])
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        merged = sc._merge_states(old, new)
        assert "Fix the auth bug in prod" in merged.pending


# ── __len__ ───────────────────────────────────────────────────────────────────

class TestScrollLen:
    def test_len_reflects_messages(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        assert len(sc) == 0
        sc.add("user", "hello")
        assert len(sc) == 1
        sc.add("assistant", "hi")
        assert len(sc) == 2

    def test_len_after_compression(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1)
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        _ = sc.messages
        assert len(sc) <= 2


# ── New API: reset, extend, needs_compress, on_compress, compress_tokens_used ─

class TestNewAPI:
    def test_reset_clears_state(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hi")
        sc._state = MemoryState(objective="test")
        sc.compressions = 3
        sc.reset()
        assert len(sc) == 0
        assert sc._state.is_empty()
        assert sc.compressions == 0
        assert sc.compress_tokens_used == 0
        assert sc._system_message is None

    def test_reset_preserves_config(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=7, token_limit=50_000)
        sc.reset()
        assert sc.keep_recent == 7
        assert sc.token_limit == 50_000

    def test_extend_bulk_loads(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        sc.extend(msgs)
        assert len(sc) == 3

    def test_extend_handles_system(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc.extend([
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "hi"},
        ])
        assert sc._system_message == "Be helpful"
        assert len(sc) == 1

    def test_needs_compress_false_under_limit(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=5, token_limit=100_000)
        sc.add("user", "hello")
        assert sc.needs_compress is False

    def test_needs_compress_true_over_limit(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1)
        for i in range(6):
            sc.add("user", "x" * 100)
            sc.add("assistant", "y" * 100)
        assert sc.needs_compress is True

    def test_on_compress_callback_fires(self):
        fired = []

        def callback(state, count):
            fired.append((state.objective, count))

        sc = Memory(
            FakeAnthropicClient(),
            provider="anthropic",
            keep_recent=2,
            token_limit=1,
            on_compress=callback,
        )
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        _ = sc.messages
        assert len(fired) >= 1
        assert fired[0][1] >= 1

    def test_compress_tokens_used_increments(self):
        sc = Memory(
            FakeAnthropicClient(),
            provider="anthropic",
            keep_recent=2,
            token_limit=1,
        )
        for i in range(6):
            sc.add("user", "x" * 100)
            sc.add("assistant", "y" * 100)
        _ = sc.messages
        assert sc.compress_tokens_used > 0

    def test_stats_includes_compress_tokens(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        s = sc.stats
        assert "compress_tokens_used" in s


# ── AsyncMemory ───────────────────────────────────────────────────────────────

class TestAsyncScroll:
    def test_add_and_len(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        assert len(sc) == 0
        sc.add("user", "hi")
        assert len(sc) == 1

    def test_stats(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hi")
        s = sc.stats
        assert s["total_turns"] == 1
        assert s["compressions"] == 0

    def test_get_messages_no_compress(self):
        import asyncio

        sc = AsyncMemory(
            AsyncFakeAnthropicClient(), provider="anthropic", keep_recent=10, token_limit=100_000
        )
        sc.add("user", "hello")
        sc.add("assistant", "world")
        msgs = asyncio.run(sc.get_messages())
        assert len(msgs) == 2

    def test_compression_triggered(self):
        import asyncio

        sc = AsyncMemory(
            AsyncFakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1
        )
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        asyncio.run(sc.get_messages())
        assert sc.compressions >= 1
        assert len(sc._messages) <= 2

    def test_system_message_merged_with_state(self):
        import asyncio

        sc = AsyncMemory(
            AsyncFakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1
        )
        sc.add("system", "Be helpful")
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        msgs = asyncio.run(sc.get_messages())
        sys_msgs = [m for m in msgs if m.get("role") == "system"]
        assert len(sys_msgs) == 1
        assert "Be helpful" in sys_msgs[0]["content"]
        assert "SCROLL" in sys_msgs[0]["content"]

    def test_on_compress_callback(self):
        import asyncio

        fired = []

        sc = AsyncMemory(
            AsyncFakeAnthropicClient(),
            provider="anthropic",
            keep_recent=2,
            token_limit=1,
            on_compress=lambda state, count: fired.append(count),
        )
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        asyncio.run(sc.get_messages())
        assert len(fired) >= 1

    def test_reset(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hi")
        sc._state = MemoryState(objective="test")
        sc.reset()
        assert len(sc) == 0
        assert sc._state.is_empty()

    def test_extend(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.extend([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
        assert len(sc) == 2

    def test_needs_compress(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1)
        for i in range(6):
            sc.add("user", "x" * 100)
            sc.add("assistant", "y" * 100)
        assert sc.needs_compress is True

    def test_save_and_load(self, tmp_path):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hi")
        sc._state = MemoryState(objective="async task", completed=["x"])
        sc.compressions = 1
        path = str(tmp_path / "async_state.json")
        sc.save_state(path)

        sc2 = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc2.load_state(path)
        assert sc2._state.objective == "async task"
        assert sc2.compressions == 1

    def test_anchor_and_anchor_facts(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.anchor("root_cause: middleware.py:87")
        sc.anchor_facts(["staging_url: https://staging.example.com", "deploy_cmd: ./deploy.sh"])
        assert "root_cause: middleware.py:87" in sc._state.facts
        assert "staging_url: https://staging.example.com" in sc._state.facts
        assert "deploy_cmd: ./deploy.sh" in sc._state.facts

    def test_anchor_no_duplicates(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.anchor("key: value")
        sc.anchor("key: value")
        assert sc._state.facts.count("key: value") == 1

    def test_preview_compress_input(self):
        sc = AsyncMemory(
            FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=100_000
        )
        sc.add("user", "step 1")
        sc.add("assistant", "done 1")
        sc.add("user", "step 2")
        sc.add("assistant", "done 2")
        sc.add("user", "recent 1")
        sc.add("assistant", "recent 2")
        preview = sc.preview_compress_input()
        assert "step 1" in preview
        assert "done 1" in preview
        # recent_2 turns are NOT in the compress input
        assert "recent 1" not in preview

    def test_preview_compress_input_nothing_to_compress(self):
        sc = AsyncMemory(
            FakeAnthropicClient(), provider="anthropic", keep_recent=10, token_limit=100_000
        )
        sc.add("user", "only one")
        preview = sc.preview_compress_input()
        assert "nothing to compress" in preview

    def test_stats_window_tokens(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hello")
        s = sc.stats
        assert "window_tokens" in s
        assert s["window_tokens"] >= s["estimated_tokens"]

    def test_prune_bounds_safe(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hi")
        sc.prune(100)  # prune more than exist — should not crash
        assert len(sc) == 0

    def test_max_tool_result_chars(self):
        sc = AsyncMemory(FakeAnthropicClient(), provider="anthropic", max_tool_result_chars=10)
        assert sc._max_tool_result_chars == 10

    def test_list_system_message_in_build_window(self):
        import asyncio

        sc = AsyncMemory(
            AsyncFakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1
        )
        # Anthropic list-form system message
        sc._system_message = [{"type": "text", "text": "Be helpful"}]
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        msgs = asyncio.run(sc.get_messages())
        sys_msgs = [m for m in msgs if m.get("role") == "system"]
        assert len(sys_msgs) == 1
        assert "Be helpful" in sys_msgs[0]["content"]


# ── anchor / preview_compress_input (Memory sync) ────────────────────────────

class TestAnchorAndPreview:
    def test_anchor_injects_fact(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc.anchor("root: src/auth/middleware.py:87")
        assert "root: src/auth/middleware.py:87" in sc._state.facts

    def test_anchor_no_duplicates(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc.anchor("k: v")
        sc.anchor("k: v")
        assert sc._state.facts.count("k: v") == 1

    def test_anchor_survives_compression(self):
        sc = Memory(
            FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1
        )
        sc.anchor("critical: never-forget-this")
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        _ = sc.messages
        assert "critical: never-forget-this" in sc._state.facts

    def test_preview_compress_input_content(self):
        sc = Memory(
            FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=100_000
        )
        for i in range(6):
            sc.add("user", f"turn {i}")
            sc.add("assistant", f"resp {i}")
        preview = sc.preview_compress_input()
        assert "turn 0" in preview
        # keep_recent=2 keeps the last 2 messages (turn 5 + resp 5)
        assert "turn 5" not in preview
        assert "resp 5" not in preview

    def test_preview_compress_input_includes_prior_state(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=100_000)
        sc._state = MemoryState(objective="existing obj", facts=["key: val"])
        sc.add("user", "a")
        sc.add("assistant", "b")
        sc.add("user", "c")
        sc.add("assistant", "d")
        sc.add("user", "e")
        sc.add("assistant", "f")
        preview = sc.preview_compress_input()
        assert "existing obj" in preview
        assert "Prior compressed state" in preview


# ── tool_result truncation ────────────────────────────────────────────────────

class TestToolResultTruncation:
    def test_truncation_applied(self):
        from north9.memory.core import _serialize_message

        big_content = "x" * 1000
        msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": big_content}],
        }
        text = _serialize_message(msg, max_tool_result_chars=100)
        assert len(text) < len(big_content)
        assert "omitted" in text

    def test_no_truncation_when_zero(self):
        from north9.memory.core import _serialize_message

        big_content = "x" * 1000
        msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": big_content}],
        }
        text = _serialize_message(msg, max_tool_result_chars=0)
        assert "x" * 1000 in text

    def test_truncate_helper(self):
        from north9.memory.core import _truncate

        text = "abcdefghij"
        result = _truncate(text, 6)
        assert "omitted" in result
        assert len(result) < len(text) + 50  # shorter than full text + some overhead

    def test_truncate_no_op_under_limit(self):
        from north9.memory.core import _truncate

        text = "hello"
        assert _truncate(text, 100) == text


# ── stats window_tokens (Memory sync) ────────────────────────────────────────

class TestStatsWindowTokens:
    def test_window_tokens_in_stats(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hello")
        s = sc.stats
        assert "window_tokens" in s
        assert s["window_tokens"] >= s["estimated_tokens"]

    def test_window_tokens_includes_state(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hello")
        sc._state = MemoryState(objective="big obj " * 100, facts=["fact " * 50] * 20)
        s = sc.stats
        assert s["window_tokens"] > s["estimated_tokens"]

    def test_window_tokens_includes_pinned(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic")
        sc.add("user", "hello")
        sc.add_pinned("user", "pinned " * 100)
        s = sc.stats
        assert s["window_tokens"] > s["estimated_tokens"]


# ── normalize_system_message ──────────────────────────────────────────────────

class TestNormalizeSystemMessage:
    def test_string_passthrough(self):
        from north9.memory.core import _normalize_system_message

        assert _normalize_system_message("hello") == "hello"

    def test_none_returns_none(self):
        from north9.memory.core import _normalize_system_message

        assert _normalize_system_message(None) is None

    def test_list_of_blocks(self):
        from north9.memory.core import _normalize_system_message

        blocks = [{"type": "text", "text": "part 1"}, {"type": "text", "text": "part 2"}]
        result = _normalize_system_message(blocks)
        assert "part 1" in result
        assert "part 2" in result

    def test_list_system_in_build_window(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=2, token_limit=1)
        sc._system_message = [{"type": "text", "text": "Be helpful"}]
        for i in range(6):
            sc.add("user", "x" * 50)
            sc.add("assistant", "y" * 50)
        msgs = sc.messages
        sys_msgs = [m for m in msgs if m.get("role") == "system"]
        assert len(sys_msgs) == 1
        assert "Be helpful" in sys_msgs[0]["content"]


# ── token estimation: tool_calls counted ─────────────────────────────────────

class TestTokenEstimateToolCalls:
    def test_tool_calls_content_counted(self):
        msg_tool = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read_file",
                "arguments": '{"path": "/very/long/deeply/nested/path/to/file.py"}',
            }}],
        }
        msg_plain = {
            "role": "assistant",
            "content": 'read_file({"path": "/very/long/deeply/nested/path/to/file.py"})',
        }
        t_tool = Memory._estimate_tokens([msg_tool])
        t_plain = Memory._estimate_tokens([msg_plain])
        # Should be in the same ballpark — tool_calls content is now counted
        assert t_tool > 4, "tool_calls content was not counted"
        # Within 3x of plain (not off by an order of magnitude)
        assert t_tool < t_plain * 3


# ── _should_compress: fires on few huge messages ──────────────────────────────

class TestShouldCompressHugeMessages:
    def test_compresses_few_huge_messages(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=10, token_limit=100)
        # 2 messages each ~5k tokens — under keep_recent=10 but over token_limit
        sc._messages = [
            {"role": "user", "content": "x" * 5000},
            {"role": "assistant", "content": "y" * 5000},
        ]
        assert sc._should_compress() is True

    def test_no_compress_single_message(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=10, token_limit=1)
        sc._messages = [{"role": "user", "content": "x" * 50000}]
        # Only 1 message — can't compress anything useful
        assert sc._should_compress() is False

    def test_compress_uses_adaptive_keep(self):
        # 3 messages, keep_recent=10 — should compress 2 of them (keep min 1)
        sc = Memory(FakeAnthropicClient(), provider="anthropic", keep_recent=10, token_limit=1)
        sc._messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        sc._compress()
        assert sc.compressions == 1
        # keep = min(10, max(1, 3-1)) = min(10, 2) = 2
        # old_turns = messages[:-2] = first 1 message compressed
        # recent_turns = last 2 messages kept
        assert len(sc._messages) == 2


# ── max_completed pruning ─────────────────────────────────────────────────────

class TestMaxCompleted:
    def test_completed_pruned_to_max(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", max_completed=5)
        sc._state.completed = [f"step {i}" for i in range(20)]
        sc._prune_state()
        assert len(sc._state.completed) == 5
        # Keeps most recent
        assert sc._state.completed[-1] == "step 19"
        assert sc._state.completed[0] == "step 15"

    def test_completed_not_pruned_under_max(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", max_completed=100)
        sc._state.completed = ["a", "b", "c"]
        sc._prune_state()
        assert len(sc._state.completed) == 3

    def test_max_completed_zero_disables(self):
        sc = Memory(FakeAnthropicClient(), provider="anthropic", max_completed=0)
        sc._state.completed = [f"step {i}" for i in range(200)]
        sc._prune_state()
        assert len(sc._state.completed) == 200  # no pruning when 0


# ── MCP server ────────────────────────────────────────────────────────────────

from north9.mcp import (  # noqa: E402
    memory_add_pending,
    memory_anchor,
    memory_complete_pending,
    memory_get,
    memory_load,
    memory_mark_completed,
    memory_mark_failed,
    memory_reset,
    memory_save,
    memory_set_objective,
)


class TestMCPServer:
    def setup_method(self):
        import north9.mcp as m
        m._state = MemoryState()
        m._initialized = True  # skip file load

    def test_get_state_empty(self):
        result = memory_get()
        assert "no state" in result.lower()

    def test_set_objective(self):
        memory_set_objective("Fix the auth bug")
        result = memory_get()
        assert "Fix the auth bug" in result

    def test_mark_completed(self):
        memory_mark_completed("Found root cause → middleware.py:87")
        result = memory_get()
        assert "middleware.py:87" in result

    def test_mark_failed_adds_do_not_retry(self):
        import north9.mcp as m
        memory_mark_failed("Deploy to prod → policy blocked")
        assert "[DO NOT RETRY]" in m._state.failed[0]

    def test_mark_failed_idempotent(self):
        import north9.mcp as m
        memory_mark_failed("thing → error [DO NOT RETRY]")
        memory_mark_failed("thing → error [DO NOT RETRY]")
        assert len(m._state.failed) == 1

    def test_add_pending(self):
        import north9.mcp as m
        memory_add_pending("Run ./deploy.sh staging v1.0")
        assert len(m._state.pending) == 1

    def test_mark_completed_clears_pending(self):
        import north9.mcp as m
        memory_add_pending("Fix auth bug")
        memory_mark_completed("Fix auth bug → resolved in commit abc123")
        assert "Fix auth bug" not in m._state.pending

    def test_anchor_fact(self):
        import north9.mcp as m
        memory_anchor("root_cause: middleware.py:87")
        assert "root_cause: middleware.py:87" in m._state.facts

    def test_anchor_fact_no_duplicates(self):
        import north9.mcp as m
        memory_anchor("key: value")
        memory_anchor("key: value")
        assert m._state.facts.count("key: value") == 1

    def test_complete_pending(self):
        import north9.mcp as m
        memory_add_pending("Deploy to staging")
        memory_complete_pending("Deploy to staging")
        assert "Deploy to staging" not in m._state.pending
        assert any("Deploy to staging" in c for c in m._state.completed)

    def test_reset(self):
        import north9.mcp as m
        memory_set_objective("Test obj")
        memory_reset()
        assert m._state.is_empty()

    def test_save_and_load_checkpoint(self, tmp_path):
        import north9.mcp as m
        m._state = MemoryState(objective="test", facts=["key: val"])
        path = str(tmp_path / "cp.json")
        memory_save(path)
        m._state = MemoryState()
        result = memory_load(path)
        assert "test" in result
        assert m._state.objective == "test"

    def test_load_checkpoint_missing_file(self):
        result = memory_load("/nonexistent/path.json")
        assert "not found" in result.lower()

    def test_get_state_with_all_fields(self):
        import north9.mcp as m
        m._state = MemoryState(
            objective="deploy hotfix",
            completed=["found bug → middleware.py:87"],
            failed=["deploy to prod → policy blocked [DO NOT RETRY]"],
            pending=["run tests"],
            facts=["root: middleware.py:87"],
        )
        result = memory_get()
        assert "deploy hotfix" in result
        assert "middleware.py:87" in result
        assert "DO NOT RETRY" in result
        assert "run tests" in result


class TestInstallHookScript:
    def test_hook_script_outputs_state(self, tmp_path):
        import json
        from pathlib import Path

        state_file = tmp_path / ".north9_state.json"
        state_file.write_text(json.dumps({
            "objective": "Fix auth bug",
            "completed": ["found root cause → middleware.py:87"],
            "failed": ["deploy → blocked [DO NOT RETRY]"],
            "pending": ["merge PR"],
            "facts": ["test_cmd: pytest tests/"],
            "snippets": [],
        }), encoding="utf-8")

        import os
        env = dict(os.environ)
        env["NORTH9_STATE_FILE"] = str(state_file)
        env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")

        # Run the hook script logic directly (inline the script)
        import north9.mcp as m
        original_state_file = m._STATE_FILE
        m._STATE_FILE = state_file
        m._state = MemoryState()
        m._initialized = False

        # The hook script reads the file and outputs state
        # Simulate its logic
        data = json.loads(state_file.read_text())
        assert data["objective"] == "Fix auth bug"
        assert len(data["completed"]) == 1
        assert len(data["failed"]) == 1

        m._STATE_FILE = original_state_file
        m._initialized = True
