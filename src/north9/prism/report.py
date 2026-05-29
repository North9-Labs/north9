"""Report: generate a standalone HTML visualisation of a session."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from .session import Frame, Session


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; line-height: 1.5; }
.header { padding: 2rem; border-bottom: 1px solid #21262d; }
.header h1 { font-size: 1.5rem; color: #58a6ff; margin-bottom: 0.5rem; }
.header .meta { font-size: 0.85rem; color: #8b949e; display: flex; gap: 2rem; flex-wrap: wrap; }
.header .meta span strong { color: #c9d1d9; }
.timeline { padding: 1.5rem 2rem; max-width: 1100px; }
.frame { margin-bottom: 1rem; border: 1px solid #21262d; border-radius: 8px; overflow: hidden; }
.frame-header { padding: 0.6rem 1rem; display: flex; align-items: center; gap: 0.75rem; cursor: pointer; user-select: none; }
.frame-header:hover { background: #161b22; }
.badge { font-size: 0.7rem; font-weight: 700; padding: 2px 7px; border-radius: 4px; letter-spacing: 0.05em; }
.badge-llm { background: #1f6feb; color: #e6edf3; }
.badge-tool { background: #388bfd22; color: #79c0ff; border: 1px solid #388bfd; }
.frame-id { color: #484f58; font-size: 0.8rem; }
.frame-title { flex: 1; font-size: 0.9rem; }
.elapsed { font-size: 0.8rem; color: #8b949e; margin-left: auto; }
.frame-body { padding: 1rem; border-top: 1px solid #21262d; display: none; }
.frame-body.open { display: block; }
.section-label { font-size: 0.75rem; font-weight: 600; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.4rem; margin-top: 0.8rem; }
.section-label:first-child { margin-top: 0; }
pre { background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 0.8rem 1rem; font-size: 0.8rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; color: #c9d1d9; }
.tokens { font-size: 0.8rem; color: #8b949e; padding-top: 0.5rem; }
.tokens strong { color: #c9d1d9; }
"""

_JS = """
document.querySelectorAll('.frame-header').forEach(h => {
  h.addEventListener('click', () => {
    h.nextElementSibling.classList.toggle('open');
  });
});
"""


def render(session: Session, path: str | Path) -> None:
    """Write a standalone HTML report for `session` to `path`."""
    path = Path(path)
    html = _build_html(session)
    path.write_text(html, encoding="utf-8")


def _build_html(session: Session) -> str:
    frames_html = "\n".join(_frame_html(f) for f in session.frames)
    total_tokens = session.total_tokens
    elapsed_s = session.total_elapsed_ms / 1000

    meta_items = [
        f"<span><strong>{len(session.frames)}</strong> frames</span>",
        f"<span><strong>{len(session.llm_frames)}</strong> LLM calls</span>",
        f"<span><strong>{len(session.tool_frames)}</strong> tool calls</span>",
        f"<span><strong>{total_tokens:,}</strong> tokens</span>",
        f"<span><strong>{elapsed_s:.2f}s</strong> elapsed</span>",
    ]
    for k, v in session.metadata.items():
        meta_items.append(f"<span><strong>{k}:</strong> {v}</span>")

    return textwrap.dedent(f"""\
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Prism — {session.session_id[:8]}</title>
      <style>{_CSS}</style>
    </head>
    <body>
    <div class="header">
      <h1>Prism Session <code>{session.session_id[:8]}</code></h1>
      <div class="meta">{''.join(meta_items)}</div>
    </div>
    <div class="timeline">
    {frames_html}
    </div>
    <script>{_JS}</script>
    </body>
    </html>
    """)


def _frame_html(frame: Frame) -> str:
    badge_class = "badge-llm" if frame.type == "llm" else "badge-tool"
    badge_text = "LLM" if frame.type == "llm" else f"TOOL:{frame.tool or '?'}"

    # Build title from input summary
    if frame.type == "llm":
        msgs = frame.input.get("messages", [])
        if msgs:
            last = msgs[-1]
            content = last.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            title = _truncate(str(content), 80)
        else:
            title = frame.input.get("model", "LLM call")
    else:
        inp = frame.input
        title = json.dumps(inp, default=str)[:80]

    # Usage stats for LLM frames
    usage_html = ""
    if frame.type == "llm":
        usage = frame.output.get("usage", {})
        if usage:
            it = usage.get("input_tokens", "?")
            ot = usage.get("output_tokens", "?")
            usage_html = (
                f'<div class="tokens">tokens: <strong>{it}</strong> in, '
                f'<strong>{ot}</strong> out</div>'
            )

    input_json = json.dumps(frame.input, indent=2, default=str)
    output_json = json.dumps(frame.output, indent=2, default=str)

    return f"""
<div class="frame">
  <div class="frame-header">
    <span class="frame-id">#{frame.id}</span>
    <span class="badge {badge_class}">{badge_text}</span>
    <span class="frame-title">{_html_escape(title)}</span>
    <span class="elapsed">{frame.elapsed_ms}ms</span>
  </div>
  <div class="frame-body">
    <div class="section-label">Input</div>
    <pre>{_html_escape(input_json)}</pre>
    <div class="section-label">Output</div>
    <pre>{_html_escape(output_json)}</pre>
    {usage_html}
  </div>
</div>"""


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
