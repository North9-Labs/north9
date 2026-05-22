"""Memory: context compression for long-running AI agents.

Keeps the last N turns verbatim; compresses older turns into structured
state (not prose). Structured format prevents hallucination and preserves
exact values — file paths, error messages, IDs — that agents need.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

# Compression prompt — structured YAML output, not prose.
# Emphasises exact preservation of values, paths, IDs, errors and code.
_COMPRESS_PROMPT_TEMPLATE = """\
You are a specialized context compressor for AI agent conversations.

Your output will be read by an AI agent to resume work in progress. Exact values are \
critical — the agent will use them directly. Paraphrasing causes the agent to hallucinate \
wrong file paths, retry failed commands, and lose track of what it has done.

Output ONLY the following YAML block. No prose. No explanation.

```yaml
objective: "<single sentence: what the agent is trying to accomplish>"

completed:
  - "<action taken> → <exact result, file path, or output value>"

failed:
  - "<what was tried> → <exact error message or reason> [DO NOT RETRY]"

pending:
  - "<next step the agent must take>"

facts:
  - "<key: exact value>"

snippets:
  - lang: "<language>"
    code: |
      <exact code the agent must see verbatim>
```

Preservation rules — CRITICAL:
- completed/failed/pending/facts must be present (use [] if empty)
- snippets is OPTIONAL — only include code the agent must reference verbatim
- Copy EXACTLY: file paths, error messages, exception types, command flags, IDs, \
hostnames, port numbers, versions, env var names, URLs, test counts (e.g. "14 passing, 2 failing")
- URLs: NEVER shorten — write the full URL. e.g. "pr_url: https://github.com/org/repo/pull/412" \
NOT "pr_number: 412"
- Shell commands: preserve the FULL command with all flags and arguments. \
e.g. "deploy_cmd: ./deploy.sh staging v1.4.2-hotfix" NOT "deployed to staging"
- For error tracebacks: preserve the exception type and message exactly, e.g. \
"KeyError: 'private_key' at auth/jwt.py:42"
- For tool call results: note the tool name and the critical part of its output
- failed list is critical: the agent MUST NOT retry these — exact failure reason must be preserved
- When in doubt, preserve the original text rather than summarizing
- facts section MUST capture: any URL that appeared, any shell command that was run, \
any file path, any test command, any policy name or rule that blocked an action

Example:
```yaml
objective: "Refactor auth middleware to use JWT and deploy to staging"

completed:
  - "Extracted verify_token() → src/auth/jwt.py:42"
  - "Ran pytest tests/auth/ → 14 passing, 0 failing"
  - "bash: git push origin feat/jwt → pushed, CI running"

failed:
  - "Switch to RS256 → KeyError: 'private_key' not in os.environ [DO NOT RETRY]"
  - "Deploy to prod → staging must pass first [DO NOT RETRY]"

pending:
  - "Wait for CI on feat/jwt to pass"
  - "Run deploy.sh staging after CI green"

facts:
  - "algorithm: HS256 (RS256 blocked — missing env var)"
  - "secret_env: JWT_SECRET"
  - "test_cmd: pytest tests/auth/ -v"
  - "staging_url: https://staging.example.com"
  - "pr_url: https://github.com/org/repo/pull/42"
  - "deploy_cmd: ./deploy.sh staging v1.2.3-hotfix"

snippets:
  - lang: "python"
    code: |
      def verify_token(token: str) -> dict:
          return jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])
```
"""



@dataclass
class MemoryState:
    """Compressed state extracted from old turns."""

    objective: str = ""
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    snippets: list[dict] = field(default_factory=list)

    def to_system_block(self) -> str:
        """Render as a system message injected before recent turns."""
        lines = ["[SCROLL: compressed prior context]", ""]
        if self.objective:
            lines += [f"OBJECTIVE: {self.objective}", ""]
        if self.completed:
            lines.append("COMPLETED:")
            for c in self.completed:
                lines.append(f"  ✓ {c}")
            lines.append("")
        if self.failed:
            lines.append("FAILED — DO NOT RETRY:")
            for f in self.failed:
                lines.append(f"  ✗ {f}")
            lines.append("")
        if self.pending:
            lines.append("PENDING:")
            for p in self.pending:
                lines.append(f"  → {p}")
            lines.append("")
        if self.facts:
            lines.append("KEY FACTS:")
            for kf in self.facts:
                lines.append(f"  • {kf}")
            lines.append("")
        if self.snippets:
            lines.append("CODE SNIPPETS:")
            for snip in self.snippets:
                lang = snip.get("lang", "")
                code = snip.get("code", "")
                lines.append(f"  ```{lang}")
                for line in code.splitlines():
                    lines.append(f"  {line}")
                lines.append("  ```")
                lines.append("")
        return "\n".join(lines).strip()

    def is_empty(self) -> bool:
        return not any(
            [
                self.objective,
                self.completed,
                self.failed,
                self.pending,
                self.facts,
                self.snippets,
            ]
        )

    def deduplicate(self) -> None:
        """Remove duplicate items while preserving order."""
        self.completed = _ordered_dedupe(self.completed)
        self.failed = _ordered_dedupe(self.failed)
        self.pending = _ordered_dedupe(self.pending)
        self.facts = _ordered_dedupe(self.facts)
        seen: set[str] = set()
        unique: list[dict] = []
        for snip in self.snippets:
            key = snip.get("code", "")
            if key and key not in seen:
                seen.add(key)
                unique.append(snip)
        self.snippets = unique

    def to_dict(self) -> dict:
        """Serialize state to a JSON-compatible dict for persistence."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MemoryState:
        """Deserialize state from a dict (e.g. loaded from JSON)."""
        return cls(
            objective=data.get("objective", ""),
            completed=list(data.get("completed", [])),
            failed=list(data.get("failed", [])),
            pending=list(data.get("pending", [])),
            facts=list(data.get("facts", [])),
            snippets=list(data.get("snippets", [])),
        )


def _ordered_dedupe(items: list[str]) -> list[str]:
    """Remove duplicate strings while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _coerce_str_list(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, list):
        return [str(v) for v in val if v is not None]
    return []


def _coerce_snippets(val: Any) -> list[dict]:
    if not val or not isinstance(val, list):
        return []
    out = []
    for item in val:
        if isinstance(item, dict):
            out.append({
                "lang": str(item.get("lang", "")),
                "code": str(item.get("code", "")),
            })
    return out


def _parse_yaml_block(text: str) -> MemoryState:
    """Parse the YAML block from compression output.

    Tries PyYAML first (more robust with special characters, multi-line values,
    and edge cases in LLM output). Falls back to a hand-rolled parser if PyYAML
    is not installed.
    """
    stripped = re.sub(r"```ya?ml\s*", "", text)
    stripped = re.sub(r"```\s*", "", stripped).strip()

    # Try PyYAML
    try:
        import yaml  # type: ignore[import]
        data = yaml.safe_load(stripped)
        if isinstance(data, dict):
            return MemoryState(
                objective=str(data.get("objective", "") or ""),
                completed=_coerce_str_list(data.get("completed")),
                failed=_coerce_str_list(data.get("failed")),
                pending=_coerce_str_list(data.get("pending")),
                facts=_coerce_str_list(data.get("facts")),
                snippets=_coerce_snippets(data.get("snippets")),
            )
    except Exception:
        pass

    # Fallback: hand-rolled parser
    text = stripped

    state = MemoryState()
    current_list: list[str] | None = None
    current_key: str | None = None
    current_snippet: dict[str, str] | None = None
    in_code_block = False
    code_indent: int | None = None
    code_lines: list[str] = []

    def _flush_snippet() -> None:
        nonlocal current_snippet, in_code_block, code_indent, code_lines
        if current_snippet is not None:
            if code_lines:
                current_snippet["code"] = "\n".join(code_lines).rstrip("\n")
            if "code" in current_snippet or "lang" in current_snippet:
                state.snippets.append(current_snippet)
            current_snippet = None
        in_code_block = False
        code_indent = None
        code_lines = []

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.rstrip()

        # Inside a code block
        if in_code_block and current_snippet is not None:
            if line == "":
                code_lines.append("")
                i += 1
                continue
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if code_indent is not None and indent >= code_indent:
                code_lines.append(line[code_indent:])
                i += 1
                continue
            else:
                _flush_snippet()
                continue

        # Top-level scalar: objective: "..."
        m = re.match(r'^(\w+):\s*"?(.+?)"?\s*$', line)
        if m and m.group(1) in ("objective",):
            _flush_snippet()
            state.objective = m.group(2).strip('"').strip("'")
            current_list = None
            current_key = None
            i += 1
            continue

        # Top-level list key: completed:
        m = re.match(r'^(\w+):\s*(\[\])?\s*$', line)
        if m and m.group(1) in ("completed", "failed", "pending", "facts", "snippets"):
            _flush_snippet()
            current_key = m.group(1)
            if current_key in ("completed", "failed", "pending", "facts"):
                current_list = getattr(state, current_key)
            else:
                current_list = None
            i += 1
            continue

        # List item for string lists
        if current_key in ("completed", "failed", "pending", "facts"):
            m = re.match(r'^\s+-\s+"?(.+?)"?\s*$', line)
            if m and current_list is not None:
                val = m.group(1).strip('"').strip("'")
                if val:
                    current_list.append(val)
                i += 1
                continue

        # Snippet list item start
        if current_key == "snippets":
            m = re.match(r'^\s+-\s+lang:\s*"?(.+?)"?\s*$', line)
            if m:
                _flush_snippet()
                current_snippet = {"lang": m.group(1).strip('"').strip("'")}
                i += 1
                continue

            if current_snippet is not None and re.match(r'^\s+code:\s*\|\s*$', line):
                stripped = line.lstrip()
                code_indent_base = len(line) - len(stripped)
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j < len(lines):
                    first_content = lines[j]
                    stripped_first = first_content.lstrip()
                    first_indent = len(first_content) - len(stripped_first)
                    if first_indent > code_indent_base:
                        code_indent = first_indent
                    else:
                        code_indent = code_indent_base + 2
                else:
                    code_indent = code_indent_base + 2
                in_code_block = True
                i += 1
                continue

        i += 1

    _flush_snippet()
    return state


def _merge_scroll_states(old: MemoryState, new: MemoryState) -> MemoryState:
    """Merge two MemoryStates, preserving old items not overridden by new."""
    merged = MemoryState()

    merged.objective = new.objective or old.objective

    merged.completed = _ordered_dedupe(old.completed + new.completed)
    merged.failed = _ordered_dedupe(old.failed + new.failed)

    # Pending: remove items resolved in new. Match prefix before "→" so
    # "Fix bug" clears when completed has "Fix bug → commit abc123".
    resolved: set[str] = set()
    for item in new.completed + new.failed:
        resolved.add(item.strip())
        prefix = item.split("→")[0].strip()
        if prefix:
            resolved.add(prefix)
    old_pending = [
        p for p in old.pending
        if p.strip() not in resolved and p.split("→")[0].strip() not in resolved
    ]
    merged.pending = _ordered_dedupe(old_pending + new.pending)

    merged.facts = _ordered_dedupe(old.facts + new.facts)

    seen_codes = {s.get("code", "") for s in new.snippets}
    merged.snippets = list(new.snippets) + [
        s for s in old.snippets if s.get("code", "") not in seen_codes
    ]

    return merged


def _normalize_system_message(content: Any) -> str | None:
    """Normalize system message to a plain string.

    Anthropic allows system messages to be a list of content blocks.
    We flatten them to a string for string concatenation with the state block.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", block.get("content", "")) or "")
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content)


def _truncate(text: str, max_chars: int) -> str:
    """Truncate long text, keeping head and tail for context."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return text[:half] + f"\n[... {omitted} chars omitted ...]\n" + text[-half:]


def _serialize_message(msg: dict, max_tool_result_chars: int = 0) -> str:
    """Serialize a message dict to plain text for the compression model.

    Args:
        max_tool_result_chars: Truncate tool results longer than this many
            characters (0 = no limit). Prevents huge file dumps from
            overwhelming the compression model.
    """
    role = msg.get("role", "?")
    content = msg.get("content")

    # OpenAI tool calls
    if role == "assistant" and "tool_calls" in msg:
        parts = [f"[{role.upper()}]"]
        if content:
            parts.append(str(content))
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            parts.append(
                f'TOOL_CALL: {tc.get("id", "?")} '
                f'{fn.get("name", "?")}({fn.get("arguments", "")})'
            )
        return "\n".join(parts)

    if role == "tool" and "tool_call_id" in msg:
        parts = [f"[TOOL_RESULT] id={msg.get('tool_call_id', '?')}"]
        result_content = msg.get("content") or ""
        if isinstance(result_content, list):
            result_content = " ".join(
                str(b.get("text", b.get("content", ""))) if isinstance(b, dict) else str(b)
                for b in result_content
            )
        parts.append(_truncate(str(result_content), max_tool_result_chars))
        return "\n".join(parts)

    # Anthropic content blocks
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_use":
                    texts.append(
                        f'TOOL_USE: {block.get("name", "?")} '
                        f'input={block.get("input", {})}'
                    )
                elif block.get("type") == "tool_result":
                    tr_content = block.get("content") or ""
                    if isinstance(tr_content, list):
                        tr_content = " ".join(
                            b.get("text", str(b)) if isinstance(b, dict) else str(b)
                            for b in tr_content
                        )
                    tr_content = _truncate(str(tr_content), max_tool_result_chars)
                    texts.append(f'TOOL_RESULT: {block.get("tool_use_id", "?")} {tr_content}')
                else:
                    texts.append(block.get("text", block.get("content", "")) or "")
            else:
                texts.append(str(block))
        content = "\n".join(texts)
    elif content is None:
        content = ""

    header = f"[{role.upper()}]"
    return f"{header}\n{content}" if content else header


class Memory:
    """Manages message history for long-running agents.

    Automatically compresses turns older than `keep_recent` when total
    estimated tokens exceed `token_limit`. Compressed state is injected
    into the system prompt — no fake user/assistant pairs, no broken
    message alternation.

    Args:
        client: Anthropic or OpenAI client instance.
        keep_recent: Number of most-recent turns to keep verbatim.
        token_limit: Estimated token threshold that triggers compression.
        compress_model: Model used for compression (use a fast/cheap model).
        provider: "anthropic" or "openai". Auto-detected if omitted.
        max_retries: Max retries for compression API calls.
        compress_chunk_size: Max tokens per compression chunk (for very long histories).
        on_compress: Optional callback called after each compression.
            Signature: ``on_compress(state: MemoryState, compressions: int) -> None``
    """

    def __init__(
        self,
        client: Any,
        keep_recent: int = 10,
        token_limit: int = 60_000,
        compress_model: str | None = None,
        provider: str | None = None,
        max_retries: int = 3,
        compress_chunk_size: int = 20_000,
        on_compress: Callable[[MemoryState, int], None] | None = None,
        compress_prompt: str | None = None,
        max_tool_result_chars: int = 4_000,
        max_completed: int = 100,
    ) -> None:
        self._client = client
        self.keep_recent = keep_recent
        self.token_limit = token_limit
        self._provider = provider or self._detect_provider(client)
        self._compress_model = compress_model or self._default_compress_model()
        self._max_retries = max_retries
        self._compress_chunk_size = compress_chunk_size
        self._on_compress = on_compress
        self._compress_prompt = compress_prompt
        self._max_tool_result_chars = max_tool_result_chars
        self._max_completed = max_completed
        self._messages: list[dict] = []
        self._pinned_messages: list[dict] = []
        self._state: MemoryState = MemoryState()
        self._system_message: str | None = None
        self.compressions: int = 0
        self.compress_tokens_used: int = 0
        self._tokens_before_last_compress: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, role: str, content: str) -> None:
        """Append a turn. Call after each user/assistant message."""
        if role == "system":
            self._system_message = content
        else:
            self._messages.append({"role": role, "content": content})

    def add_message(self, message: dict) -> None:
        """Append a raw message dict (tool calls, content blocks, etc.)."""
        if message.get("role") == "system":
            self._system_message = message.get("content")
        else:
            self._messages.append(message)

    def add_pinned(self, role: str, content: str) -> None:
        """Append a pinned message that survives every compression."""
        self._pinned_messages.append({"role": role, "content": content})

    def extend(self, messages: list[dict]) -> None:
        """Bulk-add a list of message dicts (e.g. from an existing conversation)."""
        for msg in messages:
            self.add_message(msg)

    def reset(self) -> None:
        """Clear message buffer and state. Keeps config (keep_recent, token_limit, etc.)."""
        self._messages = []
        self._state = MemoryState()
        self._system_message = None
        self.compressions = 0
        self.compress_tokens_used = 0

    def prune(self, n: int) -> None:
        """Drop the oldest n messages from the buffer without compressing them."""
        self._messages = self._messages[min(n, len(self._messages)):]

    def anchor(self, fact: str) -> None:
        """Directly inject a fact into compressed state — survives all future compressions.

        Use this for critical discoveries you don't want to risk losing:
        a root cause, a deploy blocker, a key config value. Anchored facts
        appear in KEY FACTS and are never overwritten by subsequent compressions.
        """
        if fact and fact.strip() not in self._state.facts:
            self._state.facts.append(fact.strip())

    def anchor_facts(self, facts: list[str]) -> None:
        """Anchor multiple facts at once. See anchor()."""
        for f in facts:
            self.anchor(f)

    def preview_compress_input(self) -> str:
        """Return the history text that would be sent to the compression model.

        Useful for debugging — shows exactly what the compression model sees,
        including prior state and all tool call serialization.
        """
        if len(self._messages) <= self.keep_recent:
            return "(nothing to compress — all messages within keep_recent)"
        old_turns = self._messages[: -self.keep_recent]
        parts: list[str] = []
        if not self._state.is_empty():
            parts.append("[Prior compressed state]\n" + self._state.to_system_block())
        parts.extend(_serialize_message(m, self._max_tool_result_chars) for m in old_turns)
        return "\n\n---\n\n".join(parts)

    def add_tool_use(
        self,
        name: str,
        input: dict,
        tool_id: str | None = None,
        text: str | None = None,
    ) -> str:
        """Add an Anthropic-style assistant turn containing a tool_use block.

        Returns the tool_id (auto-generated if not provided).
        """
        import uuid
        tid = tool_id or f"toolu_{uuid.uuid4().hex[:12]}"
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        content.append({"type": "tool_use", "id": tid, "name": name, "input": input})
        self._messages.append({"role": "assistant", "content": content})
        return tid

    def add_tool_result(
        self,
        tool_id: str,
        content: str | list,
        is_error: bool = False,
    ) -> None:
        """Add an Anthropic-style user turn containing a tool_result block."""
        block: dict = {"type": "tool_result", "tool_use_id": tool_id}
        if is_error:
            block["is_error"] = True
        if isinstance(content, str):
            block["content"] = content
        else:
            block["content"] = content
        self._messages.append({"role": "user", "content": [block]})

    def add_function_call(
        self,
        name: str,
        arguments: str | dict,
        call_id: str | None = None,
        text: str | None = None,
    ) -> str:
        """Add an OpenAI-style assistant turn containing a function/tool call.

        Returns the call_id (auto-generated if not provided).
        """
        import json as _json
        import uuid
        cid = call_id or f"call_{uuid.uuid4().hex[:12]}"
        args = _json.dumps(arguments) if isinstance(arguments, dict) else arguments
        msg: dict = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
            ],
        }
        self._messages.append(msg)
        return cid

    def add_function_result(self, call_id: str, content: str) -> None:
        """Add an OpenAI-style tool result message."""
        self._messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    @property
    def messages(self) -> list[dict]:
        """Current message list — auto-compresses if over token_limit."""
        if self._should_compress():
            self._compress()
        return self._build_window()

    @property
    def needs_compress(self) -> bool:
        """True if calling .messages would trigger compression."""
        return self._should_compress()

    def force_compress(self) -> MemoryState:
        """Manually trigger compression regardless of token count."""
        self._compress()
        return self._state

    @property
    def state(self) -> MemoryState:
        """Current compressed state (from last compression)."""
        return self._state

    @property
    def stats(self) -> dict:
        """Compression and token usage stats."""
        msg_tokens = self._estimate_tokens(self._messages)
        state_tokens = (
            self._estimate_tokens([{"role": "system", "content": self._state.to_system_block()}])
            if not self._state.is_empty() else 0
        )
        pinned_tokens = self._estimate_tokens(list(self._pinned_messages))
        window = msg_tokens + state_tokens + pinned_tokens
        ratio = (
            round(self._tokens_before_last_compress / max(state_tokens, 1), 1)
            if self.compressions > 0 and state_tokens > 0 else None
        )
        return {
            "total_turns": len(self._messages),
            "recent_turns_kept": min(len(self._messages), self.keep_recent),
            "compressions": self.compressions,
            "estimated_tokens": msg_tokens,
            "window_tokens": window,
            "compress_tokens_used": self.compress_tokens_used,
            "compression_ratio": ratio,
            "has_state": not self._state.is_empty(),
        }

    def __len__(self) -> int:
        """Number of turns currently in the message buffer."""
        return len(self._messages)

    def save_state(self, path: str) -> None:
        """Persist compressed state + recent messages to a JSON file.

        Use load_state() to resume after a crash or restart.
        """
        import os

        data = {
            "state": self._state.to_dict(),
            "messages": self._messages,
            "pinned_messages": self._pinned_messages,
            "system_message": self._system_message,
            "compressions": self.compressions,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def load_state(self, path: str) -> None:
        """Resume from a previously saved state file."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid state file: expected a JSON object, got {type(data).__name__}"
            )
        self._state = MemoryState.from_dict(data.get("state") or {})
        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ValueError("Invalid state file: 'messages' must be a list")
        self._messages = [m for m in raw_messages if isinstance(m, dict)]
        raw_pinned = data.get("pinned_messages", [])
        if not isinstance(raw_pinned, list):
            raise ValueError("Invalid state file: 'pinned_messages' must be a list")
        self._pinned_messages = [m for m in raw_pinned if isinstance(m, dict)]
        sys_msg = data.get("system_message")
        self._system_message = str(sys_msg) if sys_msg is not None else None
        compressions = data.get("compressions", 0)
        self.compressions = int(compressions) if isinstance(compressions, (int, float)) else 0

    # ── Internals ─────────────────────────────────────────────────────────────

    def _should_compress(self) -> bool:
        if len(self._messages) <= 1:
            return False
        # Estimate the full window: messages + state block + pinned messages
        total = self._estimate_tokens(self._messages)
        if not self._state.is_empty():
            total += self._estimate_tokens(
                [{"role": "system", "content": self._state.to_system_block()}]
            )
        total += self._estimate_tokens(list(self._pinned_messages))
        return total > self.token_limit

    def _compress(self) -> None:
        """Compress all turns older than keep_recent into structured state."""
        if len(self._messages) <= 1:
            return

        # When len < keep_recent but still over limit (e.g. a few huge file reads),
        # compress everything except the most recent message so we make progress.
        keep = min(self.keep_recent, max(1, len(self._messages) - 1))
        old_turns = self._messages[: -keep]
        recent_turns = self._messages[-keep :]

        tokens_before = self._estimate_tokens(old_turns)
        new_state = self._compress_turns(old_turns)
        self._state = self._merge_states(self._state, new_state)
        self._state.deduplicate()
        self._prune_state()
        self._messages = recent_turns
        self.compressions += 1
        self.compress_tokens_used += tokens_before
        self._tokens_before_last_compress = tokens_before

        if self._on_compress is not None:
            self._on_compress(self._state, self.compressions)

    def _compress_turns(self, turns: list[dict]) -> MemoryState:
        """Compress a list of turns, chunking if necessary."""
        total_tokens = self._estimate_tokens(turns)
        ser = lambda m: _serialize_message(m, self._max_tool_result_chars)  # noqa: E731

        if total_tokens <= self._compress_chunk_size:
            history_text = "\n\n---\n\n".join(ser(m) for m in turns)
            if not self._state.is_empty():
                history_text = (
                    "[Prior compressed state]\n"
                    + self._state.to_system_block()
                    + "\n\n---\n\n"
                    + history_text
                )
            raw = self._call_compress(history_text)
            return _parse_yaml_block(raw)

        # Chunking for very large histories
        chunks: list[list[dict]] = []
        current_chunk: list[dict] = []
        current_tokens = 0

        for msg in turns:
            msg_tokens = self._estimate_tokens([msg])
            if current_tokens + msg_tokens > self._compress_chunk_size and current_chunk:
                chunks.append(current_chunk)
                current_chunk = [msg]
                current_tokens = msg_tokens
            else:
                current_chunk.append(msg)
                current_tokens += msg_tokens

        if current_chunk:
            chunks.append(current_chunk)

        accumulated = MemoryState()
        for chunk in chunks:
            chunk_text = "\n\n---\n\n".join(ser(m) for m in chunk)
            prefix = ""
            if not accumulated.is_empty():
                prefix = (
                    "[Accumulated state so far]\n"
                    + accumulated.to_system_block()
                    + "\n\n---\n\n"
                )
            if not self._state.is_empty():
                prefix = (
                    "[Prior compressed state]\n"
                    + self._state.to_system_block()
                    + "\n\n---\n\n"
                    + prefix
                )

            raw = self._call_compress(prefix + chunk_text)
            chunk_state = _parse_yaml_block(raw)
            accumulated = self._merge_states(accumulated, chunk_state)

        return accumulated

    def _merge_states(self, old: MemoryState, new: MemoryState) -> MemoryState:
        return _merge_scroll_states(old, new)

    def _prune_state(self) -> None:
        """Keep completed list from growing unboundedly across many compressions.

        After hundreds of compressions a completed list can have thousands of
        entries. We keep the most recent max_completed items — oldest completed
        work is least relevant to resuming the current task.
        """
        if self._max_completed > 0 and len(self._state.completed) > self._max_completed:
            self._state.completed = self._state.completed[-self._max_completed:]

    def _build_window(self) -> list[dict]:
        """Build final message list: system(+state) → pinned → recent turns.

        Compressed state is merged into the system prompt rather than
        injected as a user/assistant pair — preserves message alternation
        and avoids wasting two context slots on a fake exchange.
        """
        result: list[dict] = []

        sys_text = _normalize_system_message(self._system_message)
        if not self._state.is_empty():
            state_block = self._state.to_system_block()
            system_content = (sys_text + "\n\n" + state_block) if sys_text else state_block
            result.append({"role": "system", "content": system_content})
        elif sys_text is not None:
            result.append({"role": "system", "content": sys_text})

        result.extend(list(self._pinned_messages))
        result.extend(list(self._messages))
        return result

    def _call_compress(self, history_text: str) -> str:
        """Call the compression model with retry logic. Returns raw text.

        Sends the compression instructions as a system message and the
        conversation history as the user message — produces more reliable
        structured output than a single user-only prompt.
        """
        if self._max_retries <= 0:
            raise ValueError("max_retries must be >= 1")
        system_prompt = self._compress_prompt or _COMPRESS_PROMPT_TEMPLATE
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                if self._provider == "anthropic":
                    resp = self._client.messages.create(
                        model=self._compress_model,
                        max_tokens=4096,
                        system=system_prompt,
                        messages=[{"role": "user", "content": history_text}],
                    )
                    return resp.content[0].text
                elif self._provider == "openai":
                    resp = self._client.chat.completions.create(
                        model=self._compress_model,
                        max_tokens=4096,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": history_text},
                        ],
                    )
                    return resp.choices[0].message.content or ""
                else:
                    raise ValueError(f"Unknown provider: {self._provider!r}")
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    sleep_time = (2**attempt) + random.random()
                    time.sleep(sleep_time)
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        """Estimate tokens for a list of messages.

        Uses tiktoken if available.
        Otherwise falls back to a content-aware heuristic.
        """
        if not messages:
            return 0

        # Try tiktoken
        try:
            import tiktoken

            enc = tiktoken.get_encoding("cl100k_base")
            total = 0
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = str(content)
                if content:
                    total += len(enc.encode(str(content)))
                # Count OpenAI tool_calls content (often ignored, causes undercount)
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    for part in (fn.get("name", ""), fn.get("arguments", "")):
                        if part:
                            total += len(enc.encode(str(part)))
                total += 4  # message overhead
            return total
        except Exception:
            pass

        # Fallback heuristic
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                content = str(content)

            # Collect all text to estimate from (content + tool_calls)
            all_text_parts = [str(content)] if content else []
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                all_text_parts.append(fn.get("name", "") + fn.get("arguments", ""))

            for text in all_text_parts:
                if not text:
                    continue
                ratio = 3.5

                alpha_ratio = sum(c.isalpha() or c.isspace() for c in text) / max(len(text), 1)
                if alpha_ratio > 0.85:
                    ratio = 4.0

                code_keywords = (
                    "def ", "class ", "import ", "function ",
                    "const ", "let ", "var ", "return ", "if ", "for ",
                )
                if any(kw in text for kw in code_keywords):
                    ratio = 3.0

                if (
                    sum(c in text for c in "/_.=-+0123456789") > len(text) * 0.15
                ):
                    ratio = 3.0

                total += max(1, int(len(text) / ratio))

            total += 4  # role + format overhead

        return total

    @staticmethod
    def _detect_provider(client: Any) -> str:
        module = type(client).__module__
        if "anthropic" in module:
            return "anthropic"
        if "openai" in module:
            return "openai"
        # Fallback: check for known attributes
        if hasattr(client, "messages"):
            return "anthropic"
        if hasattr(client, "chat"):
            return "openai"
        raise ValueError(
            "Cannot detect provider. Pass provider='anthropic' or provider='openai'."
        )

    def _default_compress_model(self) -> str:
        if self._provider == "anthropic":
            return "claude-haiku-4-5-20251001"
        if self._provider == "openai":
            return "gpt-4o-mini"
        return "claude-haiku-4-5-20251001"


class AsyncMemory:
    """Async version of Memory for use with AsyncAnthropic / AsyncOpenAI.

    Same API as Memory but ``get_messages()`` is a coroutine and compression
    calls are awaited, making it compatible with async agent loops.

    Usage::

        client = anthropic.AsyncAnthropic()
        sc = AsyncMemory(client, keep_recent=10, token_limit=60_000)

        sc.add("user", user_input)
        msgs = await sc.get_messages()
        response = await client.messages.create(..., messages=msgs)
    """

    def __init__(
        self,
        client: Any,
        keep_recent: int = 10,
        token_limit: int = 60_000,
        compress_model: str | None = None,
        provider: str | None = None,
        max_retries: int = 3,
        compress_chunk_size: int = 20_000,
        on_compress: Callable[[MemoryState, int], None] | None = None,
        compress_prompt: str | None = None,
        max_tool_result_chars: int = 4_000,
        max_completed: int = 100,
    ) -> None:
        self._client = client
        self.keep_recent = keep_recent
        self.token_limit = token_limit
        self._provider = provider or Memory._detect_provider(client)
        self._compress_model = compress_model or self._default_compress_model()
        self._max_retries = max_retries
        self._compress_chunk_size = compress_chunk_size
        self._on_compress = on_compress
        self._compress_prompt = compress_prompt
        self._max_tool_result_chars = max_tool_result_chars
        self._max_completed = max_completed
        self._messages: list[dict] = []
        self._pinned_messages: list[dict] = []
        self._state: MemoryState = MemoryState()
        self._system_message: str | None = None
        self.compressions: int = 0
        self.compress_tokens_used: int = 0
        self._tokens_before_last_compress: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, role: str, content: str) -> None:
        if role == "system":
            self._system_message = content
        else:
            self._messages.append({"role": role, "content": content})

    def add_message(self, message: dict) -> None:
        if message.get("role") == "system":
            self._system_message = message.get("content")
        else:
            self._messages.append(message)

    def add_pinned(self, role: str, content: str) -> None:
        self._pinned_messages.append({"role": role, "content": content})

    def extend(self, messages: list[dict]) -> None:
        for msg in messages:
            self.add_message(msg)

    def reset(self) -> None:
        self._messages = []
        self._state = MemoryState()
        self._system_message = None
        self.compressions = 0
        self.compress_tokens_used = 0

    def prune(self, n: int) -> None:
        """Drop the oldest n messages from the buffer without compressing them."""
        self._messages = self._messages[min(n, len(self._messages)):]

    def anchor(self, fact: str) -> None:
        """Directly inject a fact into compressed state — survives all future compressions."""
        if fact and fact.strip() not in self._state.facts:
            self._state.facts.append(fact.strip())

    def anchor_facts(self, facts: list[str]) -> None:
        """Anchor multiple facts at once. See anchor()."""
        for f in facts:
            self.anchor(f)

    def preview_compress_input(self) -> str:
        """Return the history text that would be sent to the compression model."""
        if len(self._messages) <= self.keep_recent:
            return "(nothing to compress — all messages within keep_recent)"
        old_turns = self._messages[: -self.keep_recent]
        parts: list[str] = []
        if not self._state.is_empty():
            parts.append("[Prior compressed state]\n" + self._state.to_system_block())
        parts.extend(_serialize_message(m, self._max_tool_result_chars) for m in old_turns)
        return "\n\n---\n\n".join(parts)

    def add_tool_use(
        self,
        name: str,
        input: dict,
        tool_id: str | None = None,
        text: str | None = None,
    ) -> str:
        import uuid
        tid = tool_id or f"toolu_{uuid.uuid4().hex[:12]}"
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        content.append({"type": "tool_use", "id": tid, "name": name, "input": input})
        self._messages.append({"role": "assistant", "content": content})
        return tid

    def add_tool_result(
        self,
        tool_id: str,
        content: str | list,
        is_error: bool = False,
    ) -> None:
        block: dict = {"type": "tool_result", "tool_use_id": tool_id}
        if is_error:
            block["is_error"] = True
        block["content"] = content
        self._messages.append({"role": "user", "content": [block]})

    def add_function_call(
        self,
        name: str,
        arguments: str | dict,
        call_id: str | None = None,
        text: str | None = None,
    ) -> str:
        import json as _json
        import uuid
        cid = call_id or f"call_{uuid.uuid4().hex[:12]}"
        args = _json.dumps(arguments) if isinstance(arguments, dict) else arguments
        msg: dict = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
            ],
        }
        self._messages.append(msg)
        return cid

    def add_function_result(self, call_id: str, content: str) -> None:
        self._messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    async def get_messages(self) -> list[dict]:
        """Return current message list, compressing if over token_limit."""
        if self._should_compress():
            await self._compress()
        return self._build_window()

    @property
    def needs_compress(self) -> bool:
        return self._should_compress()

    async def force_compress(self) -> MemoryState:
        await self._compress()
        return self._state

    @property
    def state(self) -> MemoryState:
        return self._state

    @property
    def stats(self) -> dict:
        msg_tokens = Memory._estimate_tokens(self._messages)
        state_tokens = (
            Memory._estimate_tokens([{"role": "system", "content": self._state.to_system_block()}])
            if not self._state.is_empty() else 0
        )
        pinned_tokens = Memory._estimate_tokens(list(self._pinned_messages))
        window = msg_tokens + state_tokens + pinned_tokens
        ratio = (
            round(self._tokens_before_last_compress / max(state_tokens, 1), 1)
            if self.compressions > 0 and state_tokens > 0 else None
        )
        return {
            "total_turns": len(self._messages),
            "recent_turns_kept": min(len(self._messages), self.keep_recent),
            "compressions": self.compressions,
            "estimated_tokens": msg_tokens,
            "window_tokens": window,
            "compress_tokens_used": self.compress_tokens_used,
            "compression_ratio": ratio,
            "has_state": not self._state.is_empty(),
        }

    def __len__(self) -> int:
        return len(self._messages)

    def save_state(self, path: str) -> None:
        import os

        data = {
            "state": self._state.to_dict(),
            "messages": self._messages,
            "pinned_messages": self._pinned_messages,
            "system_message": self._system_message,
            "compressions": self.compressions,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def load_state(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid state file: expected a JSON object, got {type(data).__name__}"
            )
        self._state = MemoryState.from_dict(data.get("state") or {})
        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ValueError("Invalid state file: 'messages' must be a list")
        self._messages = [m for m in raw_messages if isinstance(m, dict)]
        raw_pinned = data.get("pinned_messages", [])
        if not isinstance(raw_pinned, list):
            raise ValueError("Invalid state file: 'pinned_messages' must be a list")
        self._pinned_messages = [m for m in raw_pinned if isinstance(m, dict)]
        sys_msg = data.get("system_message")
        self._system_message = str(sys_msg) if sys_msg is not None else None
        compressions = data.get("compressions", 0)
        self.compressions = int(compressions) if isinstance(compressions, (int, float)) else 0

    # ── Internals ─────────────────────────────────────────────────────────────

    def _should_compress(self) -> bool:
        if len(self._messages) <= 1:
            return False
        total = Memory._estimate_tokens(self._messages)
        if not self._state.is_empty():
            total += Memory._estimate_tokens(
                [{"role": "system", "content": self._state.to_system_block()}]
            )
        total += Memory._estimate_tokens(list(self._pinned_messages))
        return total > self.token_limit

    async def _compress(self) -> None:
        if len(self._messages) <= 1:
            return
        keep = min(self.keep_recent, max(1, len(self._messages) - 1))
        old_turns = self._messages[: -keep]
        recent_turns = self._messages[-keep :]
        tokens_before = Memory._estimate_tokens(old_turns)
        new_state = await self._compress_turns(old_turns)
        self._state = self._merge_states(self._state, new_state)
        self._state.deduplicate()
        self._messages = recent_turns
        self.compressions += 1
        self.compress_tokens_used += tokens_before
        self._tokens_before_last_compress = tokens_before
        self._prune_state()
        if self._on_compress is not None:
            self._on_compress(self._state, self.compressions)

    def _prune_state(self) -> None:
        if self._max_completed > 0 and len(self._state.completed) > self._max_completed:
            self._state.completed = self._state.completed[-self._max_completed:]

    async def _compress_turns(self, turns: list[dict]) -> MemoryState:
        total_tokens = Memory._estimate_tokens(turns)
        ser = lambda m: _serialize_message(m, self._max_tool_result_chars)  # noqa: E731

        if total_tokens <= self._compress_chunk_size:
            history_text = "\n\n---\n\n".join(ser(m) for m in turns)
            if not self._state.is_empty():
                history_text = (
                    "[Prior compressed state]\n"
                    + self._state.to_system_block()
                    + "\n\n---\n\n"
                    + history_text
                )
            raw = await self._call_compress(history_text)
            return _parse_yaml_block(raw)

        chunks: list[list[dict]] = []
        current_chunk: list[dict] = []
        current_tokens = 0
        for msg in turns:
            msg_tokens = Memory._estimate_tokens([msg])
            if current_tokens + msg_tokens > self._compress_chunk_size and current_chunk:
                chunks.append(current_chunk)
                current_chunk = [msg]
                current_tokens = msg_tokens
            else:
                current_chunk.append(msg)
                current_tokens += msg_tokens
        if current_chunk:
            chunks.append(current_chunk)

        accumulated = MemoryState()
        for chunk in chunks:
            chunk_text = "\n\n---\n\n".join(ser(m) for m in chunk)
            prefix = ""
            if not accumulated.is_empty():
                prefix = (
                    "[Accumulated state so far]\n"
                    + accumulated.to_system_block()
                    + "\n\n---\n\n"
                )
            if not self._state.is_empty():
                prefix = (
                    "[Prior compressed state]\n"
                    + self._state.to_system_block()
                    + "\n\n---\n\n"
                    + prefix
                )
            raw = await self._call_compress(prefix + chunk_text)
            chunk_state = _parse_yaml_block(raw)
            accumulated = self._merge_states(accumulated, chunk_state)

        return accumulated

    async def _call_compress(self, history_text: str) -> str:
        if self._max_retries <= 0:
            raise ValueError("max_retries must be >= 1")
        system_prompt = self._compress_prompt or _COMPRESS_PROMPT_TEMPLATE
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                if self._provider == "anthropic":
                    resp = await self._client.messages.create(
                        model=self._compress_model,
                        max_tokens=4096,
                        system=system_prompt,
                        messages=[{"role": "user", "content": history_text}],
                    )
                    return resp.content[0].text
                elif self._provider == "openai":
                    resp = await self._client.chat.completions.create(
                        model=self._compress_model,
                        max_tokens=4096,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": history_text},
                        ],
                    )
                    return resp.choices[0].message.content or ""
                else:
                    raise ValueError(f"Unknown provider: {self._provider!r}")
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    await asyncio.sleep((2**attempt) + random.random())
        raise last_error  # type: ignore[misc]

    def _merge_states(self, old: MemoryState, new: MemoryState) -> MemoryState:
        return _merge_scroll_states(old, new)

    def _build_window(self) -> list[dict]:
        result: list[dict] = []
        sys_text = _normalize_system_message(self._system_message)
        if not self._state.is_empty():
            state_block = self._state.to_system_block()
            system_content = (sys_text + "\n\n" + state_block) if sys_text else state_block
            result.append({"role": "system", "content": system_content})
        elif sys_text is not None:
            result.append({"role": "system", "content": sys_text})
        result.extend(list(self._pinned_messages))
        result.extend(list(self._messages))
        return result

    def _default_compress_model(self) -> str:
        if self._provider == "anthropic":
            return "claude-haiku-4-5-20251001"
        if self._provider == "openai":
            return "gpt-4o-mini"
        return "claude-haiku-4-5-20251001"
