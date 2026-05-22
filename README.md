<div align="center">

# north9

**Sandboxed execution + persistent memory for AI agents.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://github.com/North9-Labs/north9/actions/workflows/ci.yml/badge.svg)](https://github.com/North9-Labs/north9/actions)

</div>

---

Two things break long-running AI agents:

1. **The agent nukes your machine.** It runs `rm -rf`, installs malware, loops forever.
2. **The agent loses its mind.** Context fills up, `/compact` turns everything into prose, and the agent forgets what it was doing, retries failed approaches, and loses every file path and error message it had.

north9 fixes both — as a single unit, because they compound each other. An isolated sandbox without persistent memory means the agent can't pick up where it left off. Persistent memory without isolation means the agent can still destroy your system.

```
Sandbox     — every command runs in Docker. Your machine is safe.
Memory      — structured state survives /compact and session restarts.
```

---

## Results

Simulated 200-turn coding session. 8 critical values tracked: file paths, error messages, URLs, commands.

```
                              Tokens    vs raw    Values preserved
─────────────────────────────────────────────────────────────────
Raw retention                  7,102      1.0x    5/8
Prose summary (/compact)         454     15.6x    0/8   (0%)
north9 memory (structured)       668     10.6x    8/8  (100%)
```

Prose compresses harder but loses every critical value. The agent can't resume meaningful work. north9 trades ~200 extra tokens to preserve all 8. After a context reset, the agent knows exactly where it left off.

---

## Claude Code setup

```sh
pip install "git+https://github.com/North9-Labs/north9.git#egg=north9[mcp]" && python3 -m north9 --install
```

Restart Claude Code. Done. Requires Docker running.

**What `--install` does:**

- Registers the north9 MCP server in `~/.claude/settings.json`
- Installs a **`PreCompact` hook** — fires before every auto-compact and `/compact`. Claude saves structured memory state before the prose summary is generated. Zero user action required.
- Installs a **`SessionStart` hook** — prior memory is auto-injected into every new session. Claude knows where it left off before you say a word.
- Creates `/project:compact`, `/project:save`, `/project:restore` commands in `.claude/commands/`
- Appends usage instructions to `CLAUDE.md`

### How auto-compact becomes lossless

Without north9, when Claude Code hits the context limit:
```
Context fills → /compact fires → everything becomes prose → file paths lost → agent starts over
```

With north9:
```
Context fills → PreCompact hook fires → Claude calls memory_mark_completed / memory_mark_failed
             → memory_anchor("root_cause: src/auth/middleware.py:87")
             → memory_save() → state written to .north9_state.json
             → /compact fires (prose summary can be brief — memory has the exact values)
             → Next session: SessionStart hook injects state automatically
             → Claude picks up exactly where it left off
```

This happens on **every** compaction — auto and manual — with no user action.

### What Claude sees at session start

```
[NORTH9] Prior session context restored:

OBJECTIVE: Fix authentication bug in prod and deploy hotfix

COMPLETED (3 total, showing last 3):
  ✓ Reproduced auth failure locally → JWT decode fails when iss claim missing
  ✓ Located root cause → src/auth/middleware.py:87 — missing issuer validation
  ✓ Ran test suite → pytest tests/auth/ → 47 passing, 0 failing

FAILED — DO NOT RETRY:
  ✗ Hotfix to prod before staging → blocked by deploy policy [DO NOT RETRY]

PENDING (do these next):
  → Merge PR #412 after CI green
  → Deploy to staging: ./deploy.sh staging v1.4.2-hotfix

KEY FACTS:
  • root_cause: src/auth/middleware.py:87
  • test_cmd: pytest tests/auth/ -v
  • pr_url: https://github.com/acme/api/pull/412

Sandbox ready — use bash(), write_file(), snapshot().
```

### MCP tools

**Sandbox** — all execution is isolated in Docker:

| Tool | Description |
|---|---|
| `bash` | Run any shell command in the container |
| `write_file` | Write a file to /workspace (visible live on host) |
| `read_file` | Read a file |
| `list_files` | List files in workspace |
| `snapshot` | Checkpoint container state (before installs/risky ops) |
| `rollback` | Restore to a snapshot |
| `export_file` | Copy a file out of the container |
| `upload_file` | Copy a host file into the container |
| `sandbox_status` | Show workspace path, container info, snapshots |

**Memory** — persists across context limits and session restarts:

| Tool | Description |
|---|---|
| `memory_get` | Read full current state |
| `memory_set_objective` | Set the session goal |
| `memory_mark_completed` | Record a completed step — include exact result |
| `memory_mark_failed` | Record a dead end — never retried |
| `memory_add_pending` | Add a next step |
| `memory_complete_pending` | Mark pending item done |
| `memory_anchor` | Pin exact values (paths, URLs, commands, errors) |
| `memory_save` | Checkpoint memory to disk |
| `memory_load` | Restore from checkpoint |
| `memory_reset` | Clear all state |

---

## Agent loop integration

If you're building your own agent loop (not Claude Code), north9 is also a library:

```sh
pip install "git+https://github.com/North9-Labs/north9.git#egg=north9"
pip install "git+https://github.com/North9-Labs/north9.git#egg=north9[anthropic]"
```

### Sandbox

```python
import north9

with north9.Sandbox(network="bridge") as env:
    env.snapshot("clean")

    result = env.run("pip install flask && python app.py", timeout=60)

    if not result.success:
        env.rollback("clean")
        print("Failed:", result.stderr)
    else:
        env.export("output.json", "./output.json")
```

The agent can `rm -rf /workspace`. Your machine is fine.

### Memory

```python
import anthropic
import north9

client = anthropic.Anthropic()
mem = north9.Memory(client, keep_recent=10, token_limit=150_000)

while not done:
    mem.add("user", get_next_input())

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8096,
        messages=mem.messages,  # compresses automatically when over limit
    )

    mem.add("assistant", response.content[0].text)
```

### Combined: sandbox + memory

```python
import anthropic
import north9

client = anthropic.Anthropic()
mem = north9.Memory(client, keep_recent=10, token_limit=150_000)

with north9.Sandbox(network="bridge") as env:
    while not done:
        mem.add("user", get_next_input())

        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=8096,
            tools=north9.TOOL_DEFINITIONS,
            messages=mem.messages,
        )

        for block in response.content:
            if block.type == "tool_use":
                result = env.handle_tool_call(block.name, block.input)
                # Tool results flow through Memory — compressed before context limit
                mem.add_tool_use(block.name, block.input, tool_id=block.id)
                mem.add_tool_result(block.id, result)
```

---

## Sandbox configuration

```python
north9.Sandbox(
    image="python:3.12-slim",  # Docker image. ubuntu:22.04 for apt+general tools.
    workspace_dir=None,        # Host path for /workspace (auto-created if None)
    name=None,                 # Container name (auto-generated if None)
    network="none",            # "none" (isolated) | "bridge" (internet access)
    memory="512m",             # Memory limit
    cpus=1.0,                  # CPU limit
    pids_limit=512,            # Fork bomb protection
    ports=None,                # {host_port: container_port} for web servers
)
```

**Network modes:**

| Mode | Use when |
|---|---|
| `"none"` (default) | No internet needed. Maximum isolation. |
| `"bridge"` | Agent needs `pip install`, `apt-get`, `npm`, API calls. |

---

## Memory configuration

```python
north9.Memory(
    client,
    keep_recent=10,                           # turns to keep verbatim
    token_limit=150_000,                      # compression threshold
    compress_model="claude-haiku-4-5-20251001",  # model for compression
    max_tool_result_chars=4_000,              # truncate large tool outputs
    max_completed=100,                        # cap completed list size
    on_compress=my_callback,                  # called after each compression
)
```

| Model | Context window | Recommended `token_limit` |
|---|---|---|
| claude-opus-4-7 | 200k | 150,000 |
| claude-sonnet-4-6 | 200k | 150,000 |
| gpt-4o | 128k | 90,000 |

---

## Snapshots and rollback

```python
with north9.Sandbox(network="bridge") as env:
    env.run("pip install -r requirements.txt")
    env.snapshot("after-install")

    env.run("python migrate.py")  # risky

    if something_went_wrong:
        env.rollback("after-install")
```

---

## Security model

| Protection | Mechanism |
|---|---|
| Filesystem isolation | Container has its own root; only `/workspace` shared with host |
| No privilege escalation | `--security-opt no-new-privileges` |
| Minimal capabilities | `--cap-drop=ALL` + only what package managers need |
| Network isolation | `network="none"` by default |
| Resource limits | `--pids-limit=512`, `--memory=512m`, `--cpus=1.0` |
| Workspace validation | Refuses to mount system paths (`/etc`, `/usr`, `/var/run`, `/`) |

---

## License

MIT — [North9 Labs](https://north9.io)
