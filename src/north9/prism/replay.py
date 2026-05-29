"""Replay: re-execute a recorded session, serving cached responses.

Three modes:
  FULL       - every frame served from recording; no live calls
  FORK       - prefix frames served from recording; pivot + tail are live
  LIVE       - only tool frames served from recording; LLM calls are live
               (useful for regression testing with real model but mocked tools)
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Callable

from .session import Fork, Frame, Session


class ReplayMode(Enum):
    FULL = "full"
    FORK = "fork"
    LIVE = "live"


class ReplayError(Exception):
    pass


class Replayer:
    """Serves frames from a recorded session in order.

    Works as a drop-in for Recorder.wrap_anthropic() — the wrapped client
    will serve recorded responses instead of making live API calls.
    """

    def __init__(self, session: Session, mode: ReplayMode = ReplayMode.FULL) -> None:
        self._frames = list(session.frames)
        self._llm_queue: list[Frame] = [f for f in self._frames if f.type == "llm"]
        self._tool_queues: dict[str, list[Frame]] = {}
        for f in self._frames:
            if f.type == "tool" and f.tool:
                self._tool_queues.setdefault(f.tool, []).append(f)
        self._llm_idx = 0
        self._tool_idxs: dict[str, int] = {}
        self.mode = mode
        self._replayed: list[Frame] = []

    # ── Frame consumption ────────────────────────────────────────────────────

    def next_llm(self) -> dict:
        """Return the next recorded LLM response."""
        if self._llm_idx >= len(self._llm_queue):
            raise ReplayError(
                f"Replay exhausted: requested LLM frame {self._llm_idx} "
                f"but only {len(self._llm_queue)} recorded."
            )
        frame = self._llm_queue[self._llm_idx]
        self._llm_idx += 1
        self._replayed.append(frame)
        return frame.output

    def next_tool(self, tool_name: str) -> Any:
        """Return the next recorded output for `tool_name`."""
        idx = self._tool_idxs.get(tool_name, 0)
        queue = self._tool_queues.get(tool_name, [])
        if idx >= len(queue):
            raise ReplayError(
                f"Replay exhausted: no recorded frame for tool '{tool_name}' at index {idx}."
            )
        frame = queue[idx]
        self._tool_idxs[tool_name] = idx + 1
        self._replayed.append(frame)
        out = frame.output
        return out.get("result", out) if isinstance(out, dict) else out

    # ── Client wrappers ──────────────────────────────────────────────────────

    def wrap_anthropic(self, client: Any) -> Any:
        """Return a wrapper that serves LLM responses from recording."""
        return _ReplayAnthropicWrapper(client, self)

    def wrap_openai(self, client: Any) -> Any:
        return _ReplayOpenAIWrapper(client, self)

    def tool(self, name: str, tool_input: dict, fn: Callable) -> Any:
        """Return recorded tool output; `fn` is never called in FULL mode."""
        if self.mode == ReplayMode.LIVE:
            return fn()
        output = self.next_tool(name)
        return output.get("result", output)

    @property
    def stats(self) -> dict:
        return {
            "llm_frames_served": self._llm_idx,
            "tool_frames_served": sum(self._tool_idxs.values()),
            "total_served": len(self._replayed),
        }


# ── Fork replay ──────────────────────────────────────────────────────────────

class ForkReplayer:
    """Replays prefix frames from recording, then switches to live execution
    at the fork point.

    The pivot frame's INPUT is replaced with fork.pivot_input.
    Frames after the fork point are live (live LLM + tool calls).

    Usage::

        session = Session.load("session.prism")
        fork = session.fork(at_frame=3, patch={"messages": [...]})
        fr = ForkReplayer(fork, recorder)
        client = fr.wrap_anthropic(anthropic.Anthropic())
        # run your agent code — first 3 frames replay, then live from frame 3
    """

    def __init__(self, fork: Fork, recorder: Any) -> None:
        self._fork = fork
        self._recorder = recorder
        # Prefix replayer for frames 0..fork_point-1
        from .session import Session

        prefix_session = Session(frames=fork.prefix_frames)
        self._prefix = Replayer(prefix_session)
        self._past_fork = False
        self._llm_count = 0
        self._prefix_llm_count = len(
            [f for f in fork.prefix_frames if f.type == "llm"]
        )

    def wrap_anthropic(self, client: Any) -> Any:
        return _ForkAnthropicWrapper(client, self)

    def tool(self, name: str, tool_input: dict, fn: Callable) -> Any:
        prefix_tool_count = len(
            [f for f in self._fork.prefix_frames if f.type == "tool" and f.tool == name]
        )
        used = self._prefix._tool_idxs.get(name, 0)
        if used < prefix_tool_count:
            return self._prefix.tool(name, tool_input, fn)
        # Past fork — live execution, record the result
        return self._recorder.tool(name, tool_input, fn)

    def _handle_llm(self, call_fn: Callable, kwargs: dict) -> Any:
        """If we're still in the prefix, serve from recording.
        At the fork point, apply the patch and go live.
        After fork, go live and record."""
        if self._llm_count < self._prefix_llm_count:
            # Serve from prefix recording
            self._llm_count += 1
            return self._prefix.next_llm()
        # At or past the fork — live
        if not self._past_fork:
            # First live call: merge patch into kwargs
            kwargs = {**kwargs, **self._fork.pivot_input}
            self._past_fork = True
        # Live call, record it
        return self._recorder.llm(call_fn, **kwargs)


# ── Anthropic replay wrapper internals ──────────────────────────────────────

class _ReplayMessages:
    def __init__(self, inner: Any, replayer: Replayer) -> None:
        self._inner = inner
        self._replayer = replayer

    def create(self, **kwargs) -> Any:
        return _DictResponse(self._replayer.next_llm())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ReplayAnthropicWrapper:
    def __init__(self, client: Any, replayer: Replayer) -> None:
        self._client = client
        self.messages = _ReplayMessages(client.messages, replayer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _ReplayOpenAIWrapper:
    def __init__(self, client: Any, replayer: Replayer) -> None:
        self._client = client
        self.chat = _ReplayChat(client.chat, replayer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _ReplayChat:
    def __init__(self, chat: Any, replayer: Replayer) -> None:
        self._inner = chat
        self.completions = _ReplayCompletions(chat.completions, replayer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ReplayCompletions:
    def __init__(self, inner: Any, replayer: Replayer) -> None:
        self._inner = inner
        self._replayer = replayer

    def create(self, **kwargs) -> Any:
        return _DictResponse(self._replayer.next_llm())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ForkMessages:
    def __init__(self, inner: Any, fork_replayer: ForkReplayer) -> None:
        self._inner = inner
        self._fr = fork_replayer

    def create(self, **kwargs) -> Any:
        return self._fr._handle_llm(self._inner.create, kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ForkAnthropicWrapper:
    def __init__(self, client: Any, fork_replayer: ForkReplayer) -> None:
        self._client = client
        self.messages = _ForkMessages(client.messages, fork_replayer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _DictResponse:
    """Thin wrapper around a recorded response dict that mimics SDK response objects."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data

    def to_dict(self) -> dict:
        return self._data

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            val = self._data[name]
            # Recurse into nested dicts
            if isinstance(val, dict):
                return _DictResponse(val)
            if isinstance(val, list):
                return [_DictResponse(v) if isinstance(v, dict) else v for v in val]
            return val
        raise AttributeError(f"Response has no attribute {name!r}")

    def __repr__(self) -> str:
        return f"_DictResponse({self._data!r})"
