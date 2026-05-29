"""Session: frame-based storage format for AI agent execution traces."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


VERSION = "0.1.0"


@dataclass
class Frame:
    """A single unit of agent execution: one LLM round-trip or one tool call."""

    id: int
    type: Literal["llm", "tool"]
    ts: float  # unix epoch seconds
    elapsed_ms: int
    input: dict[str, Any]
    output: dict[str, Any]
    # for tool frames only
    tool: str | None = None
    # arbitrary caller metadata
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "frame": self.id,
                "type": self.type,
                "ts": self.ts,
                "elapsed_ms": self.elapsed_ms,
                "tool": self.tool,
                "input": self.input,
                "output": self.output,
                "meta": self.meta,
            }
        )

    @staticmethod
    def from_dict(d: dict) -> Frame:
        return Frame(
            id=d["frame"],
            type=d["type"],
            ts=d["ts"],
            elapsed_ms=d["elapsed_ms"],
            input=d["input"],
            output=d["output"],
            tool=d.get("tool"),
            meta=d.get("meta", {}),
        )


class Session:
    """An ordered list of frames representing one complete agent execution."""

    def __init__(
        self,
        frames: list[Frame] | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.frames: list[Frame] = frames or []
        self.session_id: str = session_id or str(uuid.uuid4())
        self.metadata: dict[str, Any] = metadata or {}
        self._created_at: float = time.time()

    # ── Persistence ─────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        path = Path(path)
        with path.open("w") as f:
            # Header line
            f.write(
                json.dumps(
                    {
                        "prism": VERSION,
                        "id": self.session_id,
                        "created_at": self._created_at,
                        "frames": len(self.frames),
                        "metadata": self.metadata,
                    }
                )
                + "\n"
            )
            for frame in self.frames:
                f.write(frame.to_json() + "\n")

    @classmethod
    def load(cls, path: str | Path) -> Session:
        path = Path(path)
        with path.open() as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            raise ValueError(f"{path} is empty")
        header = json.loads(lines[0])
        if "prism" not in header:
            raise ValueError(f"{path} is not a Prism session file")
        frames = [Frame.from_dict(json.loads(l)) for l in lines[1:]]
        s = cls(
            frames=frames,
            session_id=header.get("id"),
            metadata=header.get("metadata", {}),
        )
        s._created_at = header.get("created_at", 0.0)
        return s

    # ── Inspection ───────────────────────────────────────────────────────────

    @property
    def llm_frames(self) -> list[Frame]:
        return [f for f in self.frames if f.type == "llm"]

    @property
    def tool_frames(self) -> list[Frame]:
        return [f for f in self.frames if f.type == "tool"]

    @property
    def total_tokens(self) -> int:
        total = 0
        for f in self.llm_frames:
            usage = f.output.get("usage", {})
            total += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        return total

    @property
    def total_elapsed_ms(self) -> int:
        return sum(f.elapsed_ms for f in self.frames)

    def summary(self) -> str:
        lines = [
            f"Session {self.session_id[:8]}",
            f"  frames    : {len(self.frames)} ({len(self.llm_frames)} LLM, {len(self.tool_frames)} tool)",
            f"  tokens    : {self.total_tokens:,}",
            f"  elapsed   : {self.total_elapsed_ms / 1000:.2f}s",
        ]
        if self.metadata:
            for k, v in self.metadata.items():
                lines.append(f"  {k:<9} : {v}")
        return "\n".join(lines)

    # ── Fork ─────────────────────────────────────────────────────────────────

    def fork(self, at_frame: int, patch: dict[str, Any] | None = None) -> Fork:
        """Create a fork of this session starting at `at_frame`.

        All frames before `at_frame` are replayed from recording.
        `at_frame` is replaced with `patch` applied to its input.
        Frames after `at_frame` start fresh (live execution or re-recording).
        """
        if at_frame < 0 or at_frame >= len(self.frames):
            raise IndexError(
                f"at_frame={at_frame} out of range [0, {len(self.frames) - 1}]"
            )
        prefix = self.frames[:at_frame]
        pivot = self.frames[at_frame]
        # Build patched pivot input
        new_input = {**pivot.input, **(patch or {})}
        return Fork(
            origin=self,
            fork_point=at_frame,
            prefix_frames=prefix,
            pivot_input=new_input,
            pivot_frame=pivot,
        )


@dataclass
class Fork:
    """A divergent timeline branching from an existing session at a specific frame."""

    origin: Session
    fork_point: int
    prefix_frames: list[Frame]
    pivot_input: dict[str, Any]
    pivot_frame: Frame  # original frame at fork_point

    def description(self) -> str:
        return (
            f"Fork of {self.origin.session_id[:8]} "
            f"at frame {self.fork_point} "
            f"({len(self.prefix_frames)} replayed prefix frames)"
        )
