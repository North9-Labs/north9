"""Autopsy — behavioral analysis engine for AI agent sessions.

Reads Prism session files and/or Lens trace DBs.
Detects waste patterns: dead loops, redundant reads, ignored LLM output,
always-failing tools, and token hotspots.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_COST_PER_1M: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":           (15.0, 75.0),
    "claude-sonnet-4-6":         (3.0,  15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "gpt-4o":                    (2.5,  10.0),
    "gpt-4o-mini":               (0.15, 0.60),
}

_SIMILARITY_THRESHOLD = 0.85  # Jaccard similarity for "same input" detection


@dataclass
class Finding:
    severity: str   # "critical" | "warning" | "info"
    category: str   # see _CATEGORIES
    description: str
    frame_ids: list[int]
    tokens_wasted: int = 0
    cost_usd: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def format(self) -> str:
        icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(self.severity, "⚪")
        lines = [f"{icon} [{self.category}] {self.description}"]
        if self.tokens_wasted:
            lines.append(f"   wasted ≈ {self.tokens_wasted:,} tokens (${self.cost_usd:.4f})")
        if self.frame_ids:
            lines.append(f"   frames: {self.frame_ids}")
        return "\n".join(lines)


@dataclass
class AutopsyReport:
    session_id: str
    source: str
    total_frames: int
    llm_calls: int
    tool_calls: int
    total_tokens_in: int
    total_tokens_out: int
    total_cost_usd: float
    elapsed_ms: int
    findings: list[Finding]
    tokens_by_tool: dict[str, int]    # tool → total tokens (in+out)
    calls_by_tool: dict[str, int]     # tool → call count
    errors_by_tool: dict[str, int]    # tool → error count
    avg_latency_ms: dict[str, float]  # tool → avg latency

    @property
    def total_tokens(self) -> int:
        return self.total_tokens_in + self.total_tokens_out

    @property
    def tokens_wasted(self) -> int:
        return sum(f.tokens_wasted for f in self.findings)

    @property
    def waste_pct(self) -> float:
        if not self.total_tokens:
            return 0.0
        return self.tokens_wasted / self.total_tokens * 100

    def format(self) -> str:
        lines = [
            f"Autopsy — {self.source}",
            f"session   {self.session_id[:16]}",
            f"frames    {self.total_frames}  ({self.llm_calls} LLM, {self.tool_calls} tool)",
            f"tokens    {self.total_tokens:,}  (in: {self.total_tokens_in:,}  out: {self.total_tokens_out:,})",
            f"cost      ${self.total_cost_usd:.4f}",
            f"elapsed   {self.elapsed_ms / 1000:.2f}s",
            "",
        ]

        if self.tokens_by_tool:
            lines.append("Token hotspots:")
            ranked = sorted(self.tokens_by_tool.items(), key=lambda x: x[1], reverse=True)[:10]
            for tool, toks in ranked:
                calls = self.calls_by_tool.get(tool, 0)
                errs = self.errors_by_tool.get(tool, 0)
                err_str = f"  {errs} errors" if errs else ""
                lines.append(f"  {tool:<28} {toks:>8,} tok   {calls:>4} calls{err_str}")
            lines.append("")

        if self.avg_latency_ms:
            slow = [(t, ms) for t, ms in self.avg_latency_ms.items() if ms > 2000]
            if slow:
                lines.append("Slow tools (avg > 2s):")
                for tool, ms in sorted(slow, key=lambda x: x[1], reverse=True):
                    lines.append(f"  {tool:<28} {ms/1000:.2f}s avg")
                lines.append("")

        if self.findings:
            critical = [f for f in self.findings if f.severity == "critical"]
            warnings = [f for f in self.findings if f.severity == "warning"]
            infos    = [f for f in self.findings if f.severity == "info"]

            lines.append(
                f"Findings: {len(self.findings)} total  "
                f"({len(critical)} critical, {len(warnings)} warning, {len(infos)} info)"
            )
            if self.tokens_wasted:
                lines.append(
                    f"Estimated waste: {self.tokens_wasted:,} tokens  "
                    f"({self.waste_pct:.1f}% of session)"
                )
            lines.append("")

            for f in sorted(self.findings, key=lambda x: ("critical", "warning", "info").index(x.severity)):
                lines.append(f.format())
                lines.append("")
        else:
            lines.append("No waste patterns detected.")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "source": self.source,
            "stats": {
                "frames": self.total_frames,
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.total_tokens_in,
                "tokens_out": self.total_tokens_out,
                "total_tokens": self.total_tokens,
                "cost_usd": self.total_cost_usd,
                "elapsed_ms": self.elapsed_ms,
                "tokens_wasted": self.tokens_wasted,
                "waste_pct": self.waste_pct,
            },
            "tokens_by_tool": self.tokens_by_tool,
            "calls_by_tool": self.calls_by_tool,
            "errors_by_tool": self.errors_by_tool,
            "avg_latency_ms": self.avg_latency_ms,
            "findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "description": f.description,
                    "frame_ids": f.frame_ids,
                    "tokens_wasted": f.tokens_wasted,
                    "cost_usd": f.cost_usd,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }


# ── Prism session analysis ────────────────────────────────────────────────────

def analyze_session(path: str | Path, model: str = "") -> AutopsyReport:
    """Analyze a .prism session file and return an AutopsyReport."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {path}")

    from north9.prism.session import Session
    session = Session.load(path)
    return _analyze_frames(session.frames, session.session_id, str(path), model)


def _analyze_frames(frames: list, session_id: str, source: str, model: str = "") -> AutopsyReport:
    from north9.prism.session import Frame

    llm_frames  = [f for f in frames if f.type == "llm"]
    tool_frames = [f for f in frames if f.type == "tool"]

    total_in = total_out = 0
    total_cost = 0.0
    elapsed_ms = sum(f.elapsed_ms for f in frames)

    tokens_by_tool: dict[str, int] = defaultdict(int)
    calls_by_tool:  dict[str, int] = defaultdict(int)
    errors_by_tool: dict[str, int] = defaultdict(int)
    latencies:      dict[str, list[float]] = defaultdict(list)

    for f in llm_frames:
        usage = f.output.get("usage", {})
        ti = usage.get("input_tokens", 0)
        to = usage.get("output_tokens", 0)
        total_in  += ti
        total_out += to
        m = model or f.input.get("model", "")
        total_cost += _estimate_cost(m, ti, to)
        tokens_by_tool["[llm]"] = tokens_by_tool["[llm]"] + ti + to
        calls_by_tool["[llm]"] += 1
        latencies["[llm]"].append(f.elapsed_ms)

    for f in tool_frames:
        tool = f.tool or "unknown"
        calls_by_tool[tool] += 1
        latencies[tool].append(f.elapsed_ms)
        out = f.output.get("content", "") or ""
        if isinstance(out, list):
            out = " ".join(str(x) for x in out)
        if str(out).startswith("Error:") or str(out).startswith("[exit 1]"):
            errors_by_tool[tool] = errors_by_tool.get(tool, 0) + 1

    avg_latency_ms = {t: sum(ls) / len(ls) for t, ls in latencies.items() if ls}

    findings = _detect_findings(frames, llm_frames, tool_frames, model, total_in + total_out)

    return AutopsyReport(
        session_id=session_id,
        source=source,
        total_frames=len(frames),
        llm_calls=len(llm_frames),
        tool_calls=len(tool_frames),
        total_tokens_in=total_in,
        total_tokens_out=total_out,
        total_cost_usd=total_cost,
        elapsed_ms=elapsed_ms,
        findings=findings,
        tokens_by_tool=dict(tokens_by_tool),
        calls_by_tool=dict(calls_by_tool),
        errors_by_tool=dict(errors_by_tool),
        avg_latency_ms=avg_latency_ms,
    )


def _detect_findings(frames, llm_frames, tool_frames, model, total_tokens) -> list[Finding]:
    findings: list[Finding] = []

    findings.extend(_detect_dead_loops(tool_frames, model, total_tokens))
    findings.extend(_detect_always_failing(tool_frames, model))
    findings.extend(_detect_redundant_reads(tool_frames, model, total_tokens))
    findings.extend(_detect_llm_ignored(llm_frames, tool_frames, model, total_tokens))
    findings.extend(_detect_token_hogs(llm_frames, model, total_tokens))

    return findings


def _detect_dead_loops(tool_frames, model: str, total_tokens: int) -> list[Finding]:
    """Same tool called 3+ times with similar inputs — agent is looping."""
    findings = []

    by_tool: dict[str, list] = defaultdict(list)
    for f in tool_frames:
        by_tool[f.tool or "unknown"].append(f)

    for tool, calls in by_tool.items():
        if len(calls) < 3:
            continue
        # group consecutive calls with similar inputs
        groups = _group_similar_calls(calls)
        for group in groups:
            if len(group) < 3:
                continue
            # Check if they all failed or returned identical output
            outputs = [str(f.output) for f in group]
            if len(set(outputs)) <= 2:
                waste = sum(f.elapsed_ms for f in group) * 10  # rough token estimate
                findings.append(Finding(
                    severity="critical",
                    category="dead_loop",
                    description=(
                        f"`{tool}` called {len(group)}x with similar inputs, "
                        f"same/repeated output — agent looping"
                    ),
                    frame_ids=[f.id for f in group],
                    tokens_wasted=waste,
                    cost_usd=_estimate_cost(model, waste // 2, waste // 2),
                    detail={"tool": tool, "call_count": len(group), "sample_output": outputs[0][:200]},
                ))

    return findings


def _detect_always_failing(tool_frames, model: str) -> list[Finding]:
    """Tool called multiple times and always returns an error."""
    findings = []

    by_tool: dict[str, list] = defaultdict(list)
    for f in tool_frames:
        by_tool[f.tool or "unknown"].append(f)

    for tool, calls in by_tool.items():
        if len(calls) < 2:
            continue
        errors = [
            f for f in calls
            if str(f.output.get("content", "")).startswith("Error:")
            or "[exit 1]" in str(f.output.get("content", ""))
        ]
        if len(errors) == len(calls) and len(errors) >= 2:
            findings.append(Finding(
                severity="warning",
                category="always_failing",
                description=(
                    f"`{tool}` failed {len(errors)}/{len(calls)} calls "
                    f"— never succeeded in this session"
                ),
                frame_ids=[f.id for f in calls],
                detail={"tool": tool, "error_sample": str(errors[0].output)[:200]},
            ))

    return findings


def _detect_redundant_reads(tool_frames, model: str, total_tokens: int) -> list[Finding]:
    """Same file read 3+ times — redundant I/O."""
    findings = []
    path_reads: dict[str, list] = defaultdict(list)

    for f in tool_frames:
        if f.tool not in ("read_file", "bash"):
            continue
        inp = f.input
        path = None
        if f.tool == "read_file":
            path = inp.get("path", "")
        elif f.tool == "bash":
            cmd = inp.get("command", "")
            # detect: cat /path, head /path, tail /path
            m = re.search(r'\b(?:cat|head|tail|less)\s+([\w./~-]+)', cmd)
            if m:
                path = m.group(1)
        if path:
            path_reads[path].append(f)

    for path, reads in path_reads.items():
        if len(reads) >= 3:
            findings.append(Finding(
                severity="info",
                category="redundant_read",
                description=(
                    f"`{path}` read {len(reads)}x — consider caching in memory"
                ),
                frame_ids=[f.id for f in reads],
                detail={"path": path, "read_count": len(reads)},
            ))

    return findings


def _detect_llm_ignored(llm_frames, tool_frames, model: str, total_tokens: int) -> list[Finding]:
    """LLM output immediately discarded — tool called again unchanged."""
    findings = []
    if not llm_frames or not tool_frames:
        return findings

    # Detect: LLM frame at N, tool frame at N+1 calling same tool as N-1 with same input
    all_frames = sorted(llm_frames + tool_frames, key=lambda f: f.id)

    for i in range(1, len(all_frames) - 1):
        prev = all_frames[i - 1]
        curr = all_frames[i]
        nxt  = all_frames[i + 1]

        if curr.type != "llm":
            continue
        if prev.type != "tool" or nxt.type != "tool":
            continue
        if prev.tool != nxt.tool:
            continue
        if _jaccard(str(prev.input), str(nxt.input)) > _SIMILARITY_THRESHOLD:
            usage = curr.output.get("usage", {})
            out_tokens = usage.get("output_tokens", 0)
            findings.append(Finding(
                severity="warning",
                category="llm_ignored",
                description=(
                    f"LLM output at frame {curr.id} likely ignored — "
                    f"`{nxt.tool}` called again with same input immediately after"
                ),
                frame_ids=[prev.id, curr.id, nxt.id],
                tokens_wasted=out_tokens,
                cost_usd=_estimate_cost(model, 0, out_tokens),
                detail={"llm_frame": curr.id, "tool": nxt.tool},
            ))

    return findings


def _detect_token_hogs(llm_frames, model: str, total_tokens: int) -> list[Finding]:
    """Single LLM call consuming > 20% of session tokens."""
    findings = []
    if not total_tokens:
        return findings

    for f in llm_frames:
        usage = f.output.get("usage", {})
        ti = usage.get("input_tokens", 0)
        to = usage.get("output_tokens", 0)
        frame_tokens = ti + to
        pct = frame_tokens / total_tokens * 100
        if pct > 20:
            findings.append(Finding(
                severity="warning",
                category="token_hog",
                description=(
                    f"LLM frame {f.id} consumed {frame_tokens:,} tokens "
                    f"({pct:.1f}% of session) — possible context bloat"
                ),
                frame_ids=[f.id],
                tokens_wasted=0,
                detail={"tokens_in": ti, "tokens_out": to, "pct_of_session": pct},
            ))

    return findings


# ── Lens DB analysis ──────────────────────────────────────────────────────────

def analyze_lens(
    session_id: str | None = None,
    db_path: str | Path | None = None,
    last_n: int = 100,
) -> AutopsyReport:
    """Analyze Lens trace records for a session or the most recent N calls."""
    from north9.lens.core import Tracer, _DEFAULT_DB

    db = Path(db_path) if db_path else _DEFAULT_DB
    if not db.exists():
        raise FileNotFoundError(f"Lens DB not found: {db}")

    conn = sqlite3.connect(db)
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM traces WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            ).fetchall()
            src = f"lens:{session_id[:16]}"
        else:
            rows = conn.execute(
                "SELECT * FROM traces ORDER BY timestamp DESC LIMIT ?",
                (last_n,),
            ).fetchall()
            rows = list(reversed(rows))
            src = f"lens:last_{last_n}"
    finally:
        conn.close()

    if not rows:
        raise ValueError(f"No trace records found in {db}")

    return _analyze_lens_rows(rows, src)


def _analyze_lens_rows(rows: list, source: str) -> AutopsyReport:
    # Columns: id, session_id, tool_name, input_json, output, tokens_in, tokens_out, latency_ms, timestamp, model, error
    findings: list[Finding] = []
    tokens_by_tool: dict[str, int] = defaultdict(int)
    calls_by_tool:  dict[str, int] = defaultdict(int)
    errors_by_tool: dict[str, int] = defaultdict(int)
    latency_acc:    dict[str, list[float]] = defaultdict(list)

    total_in = total_out = 0
    total_cost = 0.0
    session_ids: set[str] = set()

    for row in rows:
        (rid, sid, tool, input_json, output, ti, to, lat, ts, model, error) = row
        session_ids.add(sid)
        total_in  += ti or 0
        total_out += to or 0
        total_cost += _estimate_cost(model or "", ti or 0, to or 0)
        tok = (ti or 0) + (to or 0)
        tokens_by_tool[tool] += tok
        calls_by_tool[tool]  += 1
        latency_acc[tool].append(float(lat or 0))
        if error or (output and str(output).startswith("Error:")):
            errors_by_tool[tool] = errors_by_tool.get(tool, 0) + 1

    avg_latency_ms = {t: sum(ls) / len(ls) for t, ls in latency_acc.items() if ls}

    # Dead loop: tool called 5+ times with similar inputs
    by_tool: dict[str, list] = defaultdict(list)
    for row in rows:
        (rid, sid, tool, input_json, output, ti, to, lat, ts, model, error) = row
        by_tool[tool].append({"id": rid, "input": input_json or "", "output": output or "", "error": error})

    for tool, calls in by_tool.items():
        if len(calls) >= 5:
            outputs = [c["output"] for c in calls]
            if len(set(outputs)) <= 2:
                findings.append(Finding(
                    severity="critical",
                    category="dead_loop",
                    description=f"`{tool}` called {len(calls)}x in session with repeated output",
                    frame_ids=[],
                    detail={"tool": tool, "call_count": len(calls)},
                ))

    # Always failing
    for tool, count in calls_by_tool.items():
        err_count = errors_by_tool.get(tool, 0)
        if count >= 3 and err_count == count:
            findings.append(Finding(
                severity="warning",
                category="always_failing",
                description=f"`{tool}` failed all {count} calls in this session",
                frame_ids=[],
                detail={"tool": tool, "calls": count},
            ))

    # Token hog
    total_tokens = total_in + total_out
    for tool, toks in tokens_by_tool.items():
        pct = toks / total_tokens * 100 if total_tokens else 0
        if pct > 40:
            findings.append(Finding(
                severity="warning",
                category="token_hog",
                description=f"`{tool}` consumed {toks:,} tokens ({pct:.1f}% of session)",
                frame_ids=[],
                detail={"tool": tool, "tokens": toks, "pct": pct},
            ))

    session_id = next(iter(session_ids), "unknown")
    elapsed_ms = int(sum(float(r[7] or 0) for r in rows))

    return AutopsyReport(
        session_id=session_id,
        source=source,
        total_frames=len(rows),
        llm_calls=0,
        tool_calls=len(rows),
        total_tokens_in=total_in,
        total_tokens_out=total_out,
        total_cost_usd=total_cost,
        elapsed_ms=elapsed_ms,
        findings=findings,
        tokens_by_tool=dict(tokens_by_tool),
        calls_by_tool=dict(calls_by_tool),
        errors_by_tool=dict(errors_by_tool),
        avg_latency_ms=avg_latency_ms,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    for key, (cin, cout) in _COST_PER_1M.items():
        if key in model:
            return (tokens_in / 1_000_000 * cin) + (tokens_out / 1_000_000 * cout)
    return (tokens_in / 1_000_000 * 3.0) + (tokens_out / 1_000_000 * 15.0)


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def _group_similar_calls(frames: list) -> list[list]:
    """Group consecutive frames with similar inputs."""
    if not frames:
        return []
    groups: list[list] = [[frames[0]]]
    for f in frames[1:]:
        prev_input = str(groups[-1][-1].input)
        curr_input = str(f.input)
        if _jaccard(prev_input, curr_input) > _SIMILARITY_THRESHOLD:
            groups[-1].append(f)
        else:
            groups.append([f])
    return groups
