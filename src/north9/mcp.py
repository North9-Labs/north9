"""north9 MCP server — unified sandbox + memory tools for Claude Code.

One server. Two capabilities:

  Sandbox tools  — run commands, write files, snapshot/rollback a Docker container
  Memory tools   — persist context (completed work, failures, facts) across compactions

Quick setup:

    pip install "git+https://github.com/North9-Labs/north9.git#egg=north9[mcp]"
    python3 -m north9 --install

This registers the MCP server and installs two Claude Code hooks:
  PreCompact   — fires before every /compact and auto-compact; saves memory state
  SessionStart — injects prior state at every session start

── Sandbox config (env vars or --serve flags) ──────────────────────────────────

    NORTH9_IMAGE     Docker image (default: python:3.12-slim)
    NORTH9_NETWORK   none | bridge (default: none)
    NORTH9_MEMORY    memory limit (default: 512m)
    NORTH9_CPUS      cpu limit (default: 1.0)
    NORTH9_WORKSPACE host workspace path (default: ~/.north9/workspaces/default)
    NORTH9_NAME      container name (default: north9-default)

── Memory config ────────────────────────────────────────────────────────────────

    NORTH9_STATE_FILE path to state JSON (default: .north9_state.json)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .memory.core import MemoryState
from .sandbox.core import CageError, ExecResult, Sandbox, _default_workspace_root, _rtk_available

mcp = FastMCP(
    "north9",
    instructions=(
        "Sandboxed execution + persistent memory for long-running Claude Code sessions.\n\n"
        "SANDBOX: Every command runs in an isolated Docker container. Files in /workspace "
        "appear live on the host — open the path from sandbox_status in your editor. "
        "Snapshot before installing packages or risky operations.\n\n"
        "MEMORY: Structured state persists across context windows and session restarts. "
        "Record completed work, failed approaches, and critical facts. State is auto-injected "
        "at every session start and saved automatically before every /compact."
    ),
)

# ── Sandbox state ─────────────────────────────────────────────────────────────

_sandbox: Sandbox | None = None
_sandbox_config: dict[str, Any] = {
    "image":         os.environ.get("NORTH9_IMAGE", "python:3.12-slim"),
    "workspace_dir": os.environ.get("NORTH9_WORKSPACE", None),
    "network":       os.environ.get("NORTH9_NETWORK", "none"),
    "memory":        os.environ.get("NORTH9_MEMORY", "512m"),
    "cpus":          float(os.environ.get("NORTH9_CPUS", "1.0")),
    "name":          os.environ.get("NORTH9_NAME", "north9-default"),
    "compress":      None,
}


def configure_sandbox(**kwargs: Any) -> None:
    _sandbox_config.update(kwargs)


def _get_sandbox() -> Sandbox:
    global _sandbox
    if _sandbox is None or _sandbox._closed:
        if _sandbox_config.get("workspace_dir") is None:
            name = _sandbox_config.get("name", "north9-default")
            _sandbox_config["workspace_dir"] = _default_workspace_root() / name
        name = _sandbox_config.get("name", "north9-default")
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        _sandbox = Sandbox(**_sandbox_config)
    return _sandbox


def _fmt(result: ExecResult) -> str:
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    parts.append(f"[exit {result.exit_code}]")
    return "\n".join(parts)


# ── Memory state ──────────────────────────────────────────────────────────────

_STATE_FILE = Path(os.environ.get("NORTH9_STATE_FILE", ".north9_state.json"))
_state: MemoryState = MemoryState()
_initialized = False


def _ensure_loaded() -> None:
    global _state, _initialized
    if not _initialized:
        if _STATE_FILE.exists():
            try:
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                _state = MemoryState.from_dict(data)
            except Exception:
                _state = MemoryState()
        _initialized = True


def _save() -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(_STATE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(_STATE_FILE))


# ══════════════════════════════════════════════════════════════════════════════
# Sandbox tools
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def bash(command: str, timeout: int = 120) -> str:
    """Run a shell command in the sandboxed Docker container.

    Working directory is /workspace. Files written here appear immediately
    on the host — open the path from sandbox_status in your editor.

    Container is Debian-based (python:3.12-slim by default). Install with:
        bash("apt-get install -y git curl")
        bash("pip install flask requests")

    Snapshot before installing packages or making system changes.
    """
    try:
        return _fmt(_get_sandbox().run(command, timeout=timeout))
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write a file to the sandbox workspace.

    Files in /workspace are written directly to the host — visible immediately.

    Args:
        path:    Relative to /workspace, or absolute container path.
        content: File content.
    """
    try:
        _get_sandbox().write_file(path, content)
        return f"Wrote {path}"
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def read_file(path: str) -> str:
    """Read a file from the sandbox workspace.

    Args:
        path: Relative to /workspace, or absolute container path.
    """
    try:
        return _get_sandbox().read_file(path)
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def list_files(path: str = ".") -> str:
    """List files in the sandbox workspace.

    Args:
        path: Directory to list (default: /workspace root).
    """
    try:
        files = _get_sandbox().list_files(path)
        return "\n".join(files) if files else "(empty)"
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def snapshot(name: str = "") -> str:
    """Save a checkpoint of the container state (packages, installs, config).

    Workspace files persist independently — this only snapshots the container.
    Call before installing packages or risky system-level operations.

    Args:
        name: Optional label for this snapshot.
    """
    try:
        snap = _get_sandbox().snapshot(name or None)
        return f"Snapshot saved: {snap.name}"
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def rollback(name: str = "") -> str:
    """Restore the container to a previous snapshot.

    Reverts installed packages and system changes. Workspace files are NOT rolled back.

    Args:
        name: Snapshot to restore (default: most recent).
    """
    try:
        _get_sandbox().rollback(name or None)
        return f"Rolled back to: {name or 'most recent snapshot'}"
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def export_file(container_path: str, host_path: str) -> str:
    """Copy a file from outside /workspace to a specific host path.

    Note: /workspace files are already on the host. Use this only for files
    outside /workspace (e.g. /tmp/output, /root/.config).
    """
    try:
        _get_sandbox().export(container_path, host_path)
        return f"Exported {container_path} → {host_path}"
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def upload_file(host_path: str, container_path: str) -> str:
    """Copy a file from the host into the sandbox container.

    Args:
        host_path:      Source path on your machine.
        container_path: Destination inside the container.
    """
    try:
        _get_sandbox().upload(host_path, container_path)
        return f"Uploaded {host_path} → {container_path}"
    except CageError as e:
        return f"Error: {e}"


@mcp.tool()
def sandbox_status() -> str:
    """Show sandbox status: workspace path, container info, snapshots.

    Open the workspace path in your editor to see agent files in real time.
    """
    try:
        s = _get_sandbox()
        snaps = [sn.name for sn in s.snapshots] or ["none"]
        rtk_status = (
            "on" if s._compress else ("available" if _rtk_available() else "not installed")
        )
        lines = [
            f"workspace:  {s.workspace_path}",
            "  → open in your editor to see agent files live",
            f"container:  {s.name}",
            f"image:      {s.image}",
            f"network:    {s._network}",
            f"memory:     {s._memory}  cpus: {s._cpus}",
            f"rtk:        {rtk_status}",
            f"snapshots:  {', '.join(snaps)}",
        ]
        return "\n".join(lines)
    except CageError as e:
        return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# Memory tools
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def memory_get() -> str:
    """Get the full current memory state.

    Shows objective, completed work, failed approaches (do not retry),
    pending items, and anchored facts. Call at session start to resume work.
    """
    _ensure_loaded()
    if _state.is_empty():
        return "(no state recorded yet — use memory_set_objective to start)"
    return _state.to_system_block()


@mcp.tool()
def memory_set_objective(objective: str) -> str:
    """Set the primary goal for this session.

    One sentence: what are you trying to accomplish overall?

    Example: "Build a Flask API with JWT auth and deploy to staging"
    Example: "Migrate the user database from PostgreSQL to CockroachDB"
    """
    _ensure_loaded()
    _state.objective = objective.strip()
    _save()
    return f"Objective set: {_state.objective}"


@mcp.tool()
def memory_mark_completed(item: str) -> str:
    """Record a completed step with its exact result.

    Include BOTH the action AND the exact result — file path, output, count, URL.
    Vague entries are useless after a context reset.

    Good: "Installed Flask → pip install flask==3.0.3 → success"
    Good: "Tests passing → pytest tests/ → 47 passed, 0 failed"
    Good: "PR opened → https://github.com/acme/api/pull/412"
    Bad:  "Fixed the bug" (no specifics)

    Automatically removes matching items from pending.
    """
    _ensure_loaded()
    item = item.strip()
    if not item:
        return "Empty item — nothing recorded."
    if item not in _state.completed:
        _state.completed.append(item)
    resolved_prefix = item.split("→")[0].strip().lower()
    _state.pending = [
        p for p in _state.pending
        if p.strip() != item
        and p.split("→")[0].strip().lower() != resolved_prefix
    ]
    _save()
    return f"Completed: {item}"


@mcp.tool()
def memory_mark_failed(item: str) -> str:
    """Record a failed approach that must NOT be retried.

    Prevents wasting time on approaches already known to be broken.
    Include the exact error message or reason.

    Good: "Deploy to prod → blocked by DEPLOY_POLICY_01"
    Good: "JWT RS256 → KeyError: 'RS256_PRIVATE_KEY' not in os.environ"
    Bad:  "Tried deploying, didn't work"

    [DO NOT RETRY] tag added automatically.
    """
    _ensure_loaded()
    item = item.strip()
    if not item:
        return "Empty item — nothing recorded."
    if not item.endswith("[DO NOT RETRY]"):
        item = item + " [DO NOT RETRY]"
    if item not in _state.failed:
        _state.failed.append(item)
        _save()
    return f"Marked failed: {item}"


@mcp.tool()
def memory_add_pending(item: str) -> str:
    """Add a next step to the todo list.

    Be specific — include exact commands, file paths, or conditions.

    Good: "Run ./deploy.sh staging v1.4.2 after CI passes"
    Good: "Merge PR #412 once @sarah approves"
    Bad:  "Finish the deployment"
    """
    _ensure_loaded()
    item = item.strip()
    if not item:
        return "Empty item — nothing recorded."
    if item not in _state.pending:
        _state.pending.append(item)
        _save()
    return f"Added to pending: {item}"


@mcp.tool()
def memory_complete_pending(item: str) -> str:
    """Mark a pending item as done.

    Pass the full text or a unique prefix of the pending item.
    """
    _ensure_loaded()
    item_lower = item.strip().lower()
    matched = [p for p in _state.pending if p.strip().lower().startswith(item_lower)]
    if matched:
        for p in matched:
            _state.pending.remove(p)
            if p not in _state.completed:
                _state.completed.append(p)
        _save()
        return f"Completed {len(matched)} item(s): {'; '.join(matched)}"
    entry = item.strip()
    if entry and entry not in _state.completed:
        _state.completed.append(entry)
        _save()
    return f"Marked completed: {entry}"


@mcp.tool()
def memory_anchor(fact: str) -> str:
    """Pin a critical fact that must survive every context compression.

    Use for exact values: file paths, URLs, commands, error messages,
    config keys, test counts, hostnames — anything that must not be paraphrased.

    Good: "root_cause: src/auth/middleware.py:87"
    Good: "staging_url: https://staging.api.acme.com"
    Good: "test_cmd: pytest tests/ -v --tb=short"
    """
    _ensure_loaded()
    fact = fact.strip()
    if not fact:
        return "Empty fact — nothing recorded."
    if fact not in _state.facts:
        _state.facts.append(fact)
        _save()
    return f"Anchored: {fact}"


@mcp.tool()
def memory_save(path: str = "") -> str:
    """Save memory state to a checkpoint file.

    Call before risky operations so you can restore if something goes wrong.
    Omit path to save to the default state file.
    """
    _ensure_loaded()
    target = Path(path.strip()) if path.strip() else _STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(target) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(target))
    items = (
        len(_state.completed) + len(_state.failed)
        + len(_state.pending) + len(_state.facts)
    )
    return f"Memory saved to {target} ({items} items)"


@mcp.tool()
def memory_load(path: str) -> str:
    """Load memory state from a checkpoint file.

    Use to restore context after a crash, restart, or to pick up a previous session.
    """
    global _state
    target = Path(path.strip())
    if not target.exists():
        return f"File not found: {path}"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        _state = MemoryState.from_dict(data)
        _save()
        return f"Loaded from {path}:\n\n{_state.to_system_block()}"
    except Exception as e:
        return f"Failed to load: {e}"


@mcp.tool()
def memory_reset() -> str:
    """Clear all memory state and start fresh.

    WARNING: Deletes objective, completed work, failures, pending items, and
    anchored facts. Call memory_save first to keep a backup.
    """
    global _state
    _state = MemoryState()
    if _STATE_FILE.exists():
        try:
            _STATE_FILE.unlink()
        except OSError:
            pass
    return "Memory cleared. Starting fresh."


# ══════════════════════════════════════════════════════════════════════════════
# Install
# ══════════════════════════════════════════════════════════════════════════════

_SESSION_START_HOOK = '''\
#!/usr/bin/env python3
"""north9 SessionStart hook — injects prior memory state at session start.

Installed by: python3 -m north9 --install
"""
import json
import os
import sys
from pathlib import Path

state_file = Path(os.environ.get("NORTH9_STATE_FILE", ".north9_state.json"))

if not state_file.exists():
    sys.exit(0)

try:
    data = json.loads(state_file.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)

objective = data.get("objective", "")
completed = data.get("completed", [])
failed = data.get("failed", [])
pending = data.get("pending", [])
facts = data.get("facts", [])

if not any([objective, completed, failed, pending, facts]):
    sys.exit(0)

lines = ["[NORTH9] Prior session context restored:\\n"]

if objective:
    lines.append(f"OBJECTIVE: {objective}\\n")

if completed:
    lines.append(f"\\nCOMPLETED ({len(completed)} total, showing last 3):\\n")
    for item in completed[-3:]:
        lines.append(f"  ✓ {item}\\n")

if failed:
    lines.append("\\nFAILED — DO NOT RETRY:\\n")
    for item in failed:
        lines.append(f"  ✗ {item}\\n")

if pending:
    lines.append("\\nPENDING (do these next):\\n")
    for item in pending:
        lines.append(f"  → {item}\\n")

if facts:
    lines.append("\\nKEY FACTS:\\n")
    for item in facts:
        lines.append(f"  • {item}\\n")

lines.append("\\nCall memory_get for full details.\\n")
lines.append("Sandbox ready — use bash(), write_file(), snapshot().\\n")

print("".join(lines), end="")
'''

_PRE_COMPACT_HOOK = '''\
#!/usr/bin/env python3
"""north9 PreCompact hook — fires before every /compact and auto-compact.

Instructs Claude to save memory state before the conversation is compressed.
This makes context loss automatic and structured instead of lossy prose.

Installed by: python3 -m north9 --install
"""
import json
import os
import sys
from pathlib import Path

trigger = "auto"
try:
    data = json.loads(sys.stdin.read())
    trigger = data.get("trigger", "auto")
except Exception:
    pass

state_file = Path(os.environ.get("NORTH9_STATE_FILE", ".north9_state.json"))

lines = [
    "NORTH9 — SAVE MEMORY BEFORE COMPACTING\\n",
    "=" * 50 + "\\n\\n",
    f"Compaction triggered ({trigger}). Before proceeding:\\n\\n",
    "1. For each completed step since last save:\\n",
    "   memory_mark_completed(\\"action → exact result\\")\\n\\n",
    "2. For each failed approach:\\n",
    "   memory_mark_failed(\\"what failed → exact error\\")\\n\\n",
    "3. For next steps:\\n",
    "   memory_add_pending(\\"concrete action\\")\\n\\n",
    "4. For exact values to preserve (paths, URLs, commands, errors):\\n",
    "   memory_anchor(\\"key: exact value\\")\\n\\n",
    "5. memory_save — persist to disk\\n\\n",
    "Do this NOW, then proceed with compaction.\\n",
    "Memory reloads automatically at next session start.\\n",
]

if state_file.exists():
    try:
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        n = len(saved.get("completed", []))
        if n:
            lines.append(
                f"\\n(Already saved: {n} completed items"
                " — only add NEW work since last save)\\n"
            )
    except Exception:
        pass

print("".join(lines), end="")
'''

_CLAUDE_MD_SECTION = '''
## north9

You have the north9 MCP server — sandboxed execution + persistent memory.

### Sandbox (Docker)
All code runs in an isolated container. Your host is safe.
```
bash("command")                    # run anything
write_file("path", "content")      # create files in /workspace
read_file("path")                  # read files
snapshot("before-install")         # checkpoint container state
rollback("before-install")         # undo if something breaks
sandbox_status()                   # see workspace path + container info
```
Open the workspace path in your editor — files appear live as you work.

### Memory (persists across compactions and restarts)
State is auto-injected at session start. Update it as you work.
```
memory_mark_completed("action → exact result, file, output")
memory_mark_failed("what failed → exact error")   # never retry these
memory_add_pending("concrete next step")
memory_anchor("key: exact value")                 # paths, URLs, commands, errors
memory_save()                                     # checkpoint before risky ops
```

### Rules
- snapshot() before apt-get, pip install, or any system-level change
- memory_mark_failed() on every dead end — exact error, not vague
- memory_anchor() for any value that must survive context compression
- memory_save() before destructive operations
- Never retry items in the FAILED list
'''

_CMD_COMPACT = '''\
---
description: Save structured memory to north9 before compacting
---

Before compacting, save work to north9 memory so nothing is lost.

1. For each completed step: memory_mark_completed("action → exact result")
2. For each dead end: memory_mark_failed("what failed → exact error")
3. For next steps: memory_add_pending("concrete action")
4. For exact values: memory_anchor("key: value")
5. memory_save()
6. memory_get() — show me the saved state

Then compact normally. Memory reloads automatically next session.
'''

_CMD_SAVE = '''\
---
description: Quick save — checkpoint north9 memory state to disk
---

1. memory_save()
2. memory_get() — show a brief summary of what was saved
'''

_CMD_RESTORE = '''\
---
description: Restore north9 context after crash, restart, or /compact
---

1. memory_get() — show full state
2. Note pending items — that is where we pick up
3. Note FAILED items — do not retry these
4. Confirm you have reviewed state before continuing
'''


def _install() -> None:
    import sys

    python_exe = sys.executable
    print("Installing north9 into Claude Code...\n")

    hooks_dir = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    session_start_path = hooks_dir / "north9-session-start.py"
    session_start_path.write_text(_SESSION_START_HOOK, encoding="utf-8")
    session_start_path.chmod(0o755)
    print(f"  ✓ SessionStart hook: {session_start_path}")

    pre_compact_path = hooks_dir / "north9-pre-compact.py"
    pre_compact_path.write_text(_PRE_COMPACT_HOOK, encoding="utf-8")
    pre_compact_path.chmod(0o755)
    print(f"  ✓ PreCompact hook:   {pre_compact_path}")

    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if "mcpServers" not in settings:
        settings["mcpServers"] = {}
    settings["mcpServers"]["north9"] = {
        "command": python_exe,
        "args": ["-m", "north9"],
    }

    if "hooks" not in settings:
        settings["hooks"] = {}

    session_start = settings["hooks"].get("SessionStart", [])
    ss_cmd = f'"{python_exe}" "{session_start_path}"'
    if not any(
        h.get("command", "") == ss_cmd
        for entry in session_start
        for h in entry.get("hooks", [])
    ):
        session_start.append({"hooks": [{"type": "command", "command": ss_cmd, "timeout": 5}]})
        settings["hooks"]["SessionStart"] = session_start

    pre_compact = settings["hooks"].get("PreCompact", [])
    pc_cmd = f'"{python_exe}" "{pre_compact_path}"'
    if not any(
        h.get("command", "") == pc_cmd
        for entry in pre_compact
        for h in entry.get("hooks", [])
    ):
        pre_compact.append({"hooks": [{"type": "command", "command": pc_cmd, "timeout": 10}]})
        settings["hooks"]["PreCompact"] = pre_compact

    tmp = str(settings_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(settings_path))
    print(f"  ✓ Claude Code settings: {settings_path}")
    print("      north9 MCP server registered")
    print("      SessionStart hook (auto-injects prior memory)")
    print("      PreCompact hook (saves memory before every /compact)")

    commands_dir = Path.cwd() / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "compact.md").write_text(_CMD_COMPACT, encoding="utf-8")
    (commands_dir / "save.md").write_text(_CMD_SAVE, encoding="utf-8")
    (commands_dir / "restore.md").write_text(_CMD_RESTORE, encoding="utf-8")
    print(f"  ✓ Commands: {commands_dir}")
    print("      /project:compact  /project:save  /project:restore")

    claude_md = Path.cwd() / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## north9" not in existing:
            claude_md.write_text(existing.rstrip() + "\n" + _CLAUDE_MD_SECTION, encoding="utf-8")
            print("  ✓ CLAUDE.md updated")
        else:
            print("  ✓ CLAUDE.md already has north9 section (skipped)")
    else:
        claude_md.write_text(_CLAUDE_MD_SECTION.lstrip(), encoding="utf-8")
        print("  ✓ CLAUDE.md created")

    print("\nDone. Restart Claude Code to activate.\n")
    print("What happens now:")
    print("  • Every /compact and auto-compact → PreCompact hook fires")
    print("    Claude saves memory (completed work, failures, facts) before compacting")
    print("  • Every session start → prior memory auto-injected into context")
    print("    Claude knows where it left off before you say a word")
    print("  • All commands run in an isolated Docker container")
    print("    Agent can rm -rf, install malware, loop forever — your machine is fine")
    print()
    print("Requires Docker running. Test with: docker ps")
    print("Workspace: ~/.north9/workspaces/default/")
    print("Memory:    .north9_state.json (in your project)")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="north9",
        description="north9 MCP server — sandbox + memory for Claude Code",
    )
    parser.add_argument("--install", action="store_true",
                        help="Install north9 into Claude Code")
    parser.add_argument("--image", default=None,
                        help="Docker image (default: python:3.12-slim)")
    parser.add_argument("--network", default=None, choices=["none", "bridge"],
                        help="Network mode (default: none)")
    parser.add_argument("--memory", default=None,
                        help="Memory limit (default: 512m)")
    parser.add_argument("--cpus", default=None, type=float,
                        help="CPU limit (default: 1.0)")
    parser.add_argument("--workspace", default=None,
                        help="Host workspace path")
    parser.add_argument("--name", default=None,
                        help="Container name (default: north9-default)")
    parser.add_argument("--state-file", default=None,
                        help="Memory state file (default: .north9_state.json)")
    args, _ = parser.parse_known_args()

    if args.install:
        _install()
        return

    if args.state_file:
        global _STATE_FILE
        _STATE_FILE = Path(args.state_file)

    cfg: dict = {}
    if args.image:
        cfg["image"] = args.image
    if args.network:
        cfg["network"] = args.network
    if args.memory:
        cfg["memory"] = args.memory
    if args.cpus:
        cfg["cpus"] = args.cpus
    if args.workspace:
        cfg["workspace_dir"] = args.workspace
    if args.name:
        cfg["name"] = args.name
    if cfg:
        configure_sandbox(**cfg)

    _ensure_loaded()
    mcp.run()


if __name__ == "__main__":
    main()
