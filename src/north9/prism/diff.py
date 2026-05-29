"""Diff: compare two sessions frame-by-frame and report divergences."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .session import Frame, Session


@dataclass
class FrameDiff:
    frame_id: int
    type: str
    field: str
    left: Any
    right: Any

    def __str__(self) -> str:
        return (
            f"  frame {self.frame_id} [{self.type}] .{self.field}\n"
            f"    - {_truncate(self.left)}\n"
            f"    + {_truncate(self.right)}"
        )


@dataclass
class SessionDiff:
    left_id: str
    right_id: str
    frame_diffs: list[FrameDiff]
    left_only: list[Frame]  # frames in left but not right
    right_only: list[Frame]  # frames in right but not left
    fork_point: int | None  # first frame where they diverge

    def is_identical(self) -> bool:
        return not self.frame_diffs and not self.left_only and not self.right_only

    def summary(self) -> str:
        if self.is_identical():
            return "Sessions are identical."

        lines = [
            f"Diff: {self.left_id[:8]} → {self.right_id[:8]}",
        ]

        if self.fork_point is not None:
            lines.append(f"  fork_point : frame {self.fork_point}")

        if self.frame_diffs:
            lines.append(f"  changed    : {len(self.frame_diffs)} field(s)")
            for fd in self.frame_diffs[:10]:  # show at most 10
                lines.append(str(fd))
            if len(self.frame_diffs) > 10:
                lines.append(f"  ... and {len(self.frame_diffs) - 10} more")

        if self.left_only:
            lines.append(f"  left_only  : {len(self.left_only)} frame(s)")
        if self.right_only:
            lines.append(f"  right_only : {len(self.right_only)} frame(s)")

        return "\n".join(lines)


def diff(left: Session, right: Session) -> SessionDiff:
    """Compare two sessions and return a structured diff."""
    n = min(len(left.frames), len(right.frames))
    diffs: list[FrameDiff] = []
    fork_point: int | None = None

    for i in range(n):
        lf, rf = left.frames[i], right.frames[i]
        frame_diffs = _diff_frames(lf, rf)
        if frame_diffs:
            if fork_point is None:
                fork_point = i
            diffs.extend(frame_diffs)

    left_only = left.frames[n:]
    right_only = right.frames[n:]
    if not diffs and (left_only or right_only) and fork_point is None:
        fork_point = n

    return SessionDiff(
        left_id=left.session_id,
        right_id=right.session_id,
        frame_diffs=diffs,
        left_only=left_only,
        right_only=right_only,
        fork_point=fork_point,
    )


def _diff_frames(left: Frame, right: Frame) -> list[FrameDiff]:
    diffs = []
    if left.type != right.type:
        diffs.append(FrameDiff(left.id, left.type, "type", left.type, right.type))
        return diffs  # no point comparing further

    # Compare inputs and outputs via JSON-serialised flat comparison
    for field_name, lv, rv in [
        ("input", left.input, right.input),
        ("output", left.output, right.output),
    ]:
        if lv != rv:
            diffs.append(FrameDiff(left.id, left.type, field_name, lv, rv))

    return diffs


def _truncate(value: Any, max_len: int = 120) -> str:
    s = json.dumps(value, default=str)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s
