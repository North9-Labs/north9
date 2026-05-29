"""Capture: intercept Anthropic / OpenAI SDK calls and record them as frames."""

from __future__ import annotations

import functools
import time
from contextlib import contextmanager
from typing import Any, Callable, Generator

from .session import Frame, Session


class Recorder:
    """Wraps a session and provides methods to record LLM and tool calls."""

    def __init__(self, session: Session | None = None) -> None:
        self._session = session or Session()
        self._frame_counter = 0

    @property
    def session(self) -> Session:
        return self._session

    def _next_id(self) -> int:
        fid = self._frame_counter
        self._frame_counter += 1
        return fid

    # ── Low-level recording ──────────────────────────────────────────────────

    def record_llm(self, input_params: dict, output: dict, elapsed_ms: int) -> Frame:
        frame = Frame(
            id=self._next_id(),
            type="llm",
            ts=time.time(),
            elapsed_ms=elapsed_ms,
            input=input_params,
            output=output,
        )
        self._session.frames.append(frame)
        return frame

    def record_tool(
        self,
        name: str,
        tool_input: dict,
        tool_output: Any,
        elapsed_ms: int,
    ) -> Frame:
        frame = Frame(
            id=self._next_id(),
            type="tool",
            ts=time.time(),
            elapsed_ms=elapsed_ms,
            tool=name,
            input=tool_input,
            output={"result": tool_output} if not isinstance(tool_output, dict) else tool_output,
        )
        self._session.frames.append(frame)
        return frame

    # ── High-level helpers ───────────────────────────────────────────────────

    def llm(self, fn: Callable, *args, **kwargs) -> Any:
        """Call `fn(*args, **kwargs)`, record the LLM round-trip, return the result."""
        t0 = time.monotonic()
        result = fn(*args, **kwargs)
        elapsed = int((time.monotonic() - t0) * 1000)

        # Normalise input
        input_params = dict(kwargs)
        if args:
            input_params["_args"] = list(args)

        # Normalise output — try to extract a dict from common SDK response types
        output = _normalise_response(result)
        self.record_llm(input_params, output, elapsed)
        return result

    def tool(self, name: str, tool_input: dict, fn: Callable) -> Any:
        """Call `fn()`, record as a tool call, return the result."""
        t0 = time.monotonic()
        result = fn()
        elapsed = int((time.monotonic() - t0) * 1000)
        self.record_tool(name, tool_input, result, elapsed)
        return result

    # ── Anthropic wrapper ────────────────────────────────────────────────────

    def wrap_anthropic(self, client: Any) -> Any:
        """Return a thin wrapper around an `anthropic.Anthropic` client that
        records every `messages.create` call.  All other attributes pass through."""
        return _AnthropicWrapper(client, self)

    # ── OpenAI wrapper ───────────────────────────────────────────────────────

    def wrap_openai(self, client: Any) -> Any:
        """Return a thin wrapper around an `openai.OpenAI` client that records
        every `chat.completions.create` call."""
        return _OpenAIWrapper(client, self)

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        self._session.save(path)


# ── Anthropic wrapper internals ─────────────────────────────────────────────

class _WrappedMessages:
    def __init__(self, messages_obj: Any, recorder: Recorder) -> None:
        self._inner = messages_obj
        self._recorder = recorder

    def create(self, **kwargs) -> Any:
        t0 = time.monotonic()
        result = self._inner.create(**kwargs)
        elapsed = int((time.monotonic() - t0) * 1000)
        self._recorder.record_llm(kwargs, _normalise_response(result), elapsed)
        return result

    def stream(self, **kwargs):
        """Stream wrapper — accumulates the full message then records."""
        with self._inner.stream(**kwargs) as stream:
            # Let the caller consume the stream
            yield stream
        # After stream context exits the message is complete
        if hasattr(stream, "get_final_message"):
            msg = stream.get_final_message()
            self._recorder.record_llm(kwargs, _normalise_response(msg), 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AnthropicWrapper:
    def __init__(self, client: Any, recorder: Recorder) -> None:
        self._client = client
        self._recorder = recorder
        self.messages = _WrappedMessages(client.messages, recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


# ── OpenAI wrapper internals ─────────────────────────────────────────────────

class _WrappedCompletions:
    def __init__(self, completions_obj: Any, recorder: Recorder) -> None:
        self._inner = completions_obj
        self._recorder = recorder

    def create(self, **kwargs) -> Any:
        t0 = time.monotonic()
        result = self._inner.create(**kwargs)
        elapsed = int((time.monotonic() - t0) * 1000)
        self._recorder.record_llm(kwargs, _normalise_response(result), elapsed)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _WrappedChat:
    def __init__(self, chat_obj: Any, recorder: Recorder) -> None:
        self._inner = chat_obj
        self.completions = _WrappedCompletions(chat_obj.completions, recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _OpenAIWrapper:
    def __init__(self, client: Any, recorder: Recorder) -> None:
        self._client = client
        self._recorder = recorder
        self.chat = _WrappedChat(client.chat, recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


# ── Context manager convenience API ─────────────────────────────────────────

@contextmanager
def record(
    path: str | None = None,
    metadata: dict | None = None,
) -> Generator[Recorder, None, None]:
    """Context manager: record all LLM + tool calls within the block.

    Usage::

        with prism.record("session.prism") as rec:
            client = rec.wrap_anthropic(anthropic.Anthropic())
            response = client.messages.create(...)

    If `path` is given the session is auto-saved on exit.
    """
    from .session import Session

    session = Session(metadata=metadata or {})
    recorder = Recorder(session)
    try:
        yield recorder
    finally:
        if path:
            recorder.save(path)


# ── Response normalisation ───────────────────────────────────────────────────

def _normalise_response(result: Any) -> dict:
    """Convert an SDK response object to a plain dict for storage."""
    if isinstance(result, dict):
        return result
    # Anthropic/OpenAI SDK objects implement model_dump() (pydantic)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    # Older SDKs
    if hasattr(result, "to_dict"):
        return result.to_dict()
    # Fallback: try __dict__
    if hasattr(result, "__dict__"):
        try:
            import json
            # Round-trip through JSON to strip non-serialisable attrs
            return json.loads(json.dumps(result.__dict__, default=str))
        except Exception:
            pass
    return {"_raw": str(result)}
