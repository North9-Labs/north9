"""Prism CLI: inspect, replay, diff, and report on session files."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .session import Session
from .diff import diff as session_diff
from .report import render as render_report


@click.group()
@click.version_option()
def cli() -> None:
    """Prism — time-travel debugger for AI agents.\n
    Record, replay, fork, and diff any agent session.
    """


@cli.command()
@click.argument("path", type=click.Path(exists=True))
def inspect(path: str) -> None:
    """Print a summary of a .prism session file."""
    session = Session.load(path)
    click.echo(session.summary())
    click.echo()
    for frame in session.frames:
        icon = "🧠" if frame.type == "llm" else "🔧"
        label = f"frame {frame.id:>3}  {icon} {frame.type:<5}"
        if frame.tool:
            label += f":{frame.tool}"
        label += f"  {frame.elapsed_ms}ms"
        click.echo(label)


@cli.command()
@click.argument("session_path", type=click.Path(exists=True))
@click.option(
    "--output", "-o", default=None, help="Output HTML path (default: <session>.html)"
)
def report(session_path: str, output: str | None) -> None:
    """Generate a standalone HTML visualisation of a session."""
    session = Session.load(session_path)
    out = output or str(Path(session_path).with_suffix(".html"))
    render_report(session, out)
    click.echo(f"report written → {out}")


@cli.command("diff")
@click.argument("left", type=click.Path(exists=True))
@click.argument("right", type=click.Path(exists=True))
def diff_cmd(left: str, right: str) -> None:
    """Show the diff between two .prism session files."""
    left_session = Session.load(left)
    right_session = Session.load(right)
    result = session_diff(left_session, right_session)
    click.echo(result.summary())
    sys.exit(0 if result.is_identical() else 1)


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--frame", "-f", type=int, default=None, help="Dump a specific frame")
def dump(path: str, frame: int | None) -> None:
    """Dump raw JSON of a session or a single frame."""
    import json

    session = Session.load(path)
    if frame is not None:
        if frame >= len(session.frames):
            click.echo(f"Error: frame {frame} out of range (0-{len(session.frames)-1})", err=True)
            sys.exit(1)
        click.echo(session.frames[frame].to_json())
    else:
        for f in session.frames:
            click.echo(f.to_json())


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--at", "-a", required=True, type=int, help="Fork at this frame index")
@click.option("--patch", "-p", default=None, help="JSON patch applied to pivot frame input")
@click.option("--output", "-o", default=None, help="Save fork plan to this path")
def fork(path: str, at: int, patch: str | None, output: str | None) -> None:
    """Describe or save a fork plan without executing it."""
    import json

    session = Session.load(path)
    patch_dict = json.loads(patch) if patch else {}
    f = session.fork(at_frame=at, patch=patch_dict)
    click.echo(f.description())
    click.echo(f"  prefix frames : {len(f.prefix_frames)}")
    click.echo(f"  pivot input   : {json.dumps(f.pivot_input, default=str)[:200]}")
    if output:
        import dataclasses, json as _json
        data = {
            "origin": session.session_id,
            "fork_point": f.fork_point,
            "pivot_input": f.pivot_input,
        }
        Path(output).write_text(_json.dumps(data, indent=2))
        click.echo(f"fork plan saved → {output}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
