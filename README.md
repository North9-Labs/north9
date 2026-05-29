<div align="center">

# north9

**The complete runtime for autonomous AI agents.**

Safe execution. Persistent memory. Observability. Policy. All in one package.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/North9-Labs/north9/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/North9-Labs/north9/actions)

</div>

---

Four things kill serious AI agents:

**They destroy things.** No wall between the agent and your machine. One `rm -rf` and it's gone.

**They forget everything.** Context fills up. `/compact` turns exact file paths and error messages into vague prose. The agent retries approaches it already failed.

**They go rogue.** No cost limits. No policy enforcement. No record of what they actually did.

**You can't debug them.** When something goes wrong you have no trace, no replay, no diff — just a broken state and a blank conversation.

north9 solves all four.

```
sandbox   every command runs in Docker — your machine is always safe
memory    structured state survives /compact and session restarts
12 tools  observability, policy, eval, secrets, parallelism, and more
```

---

## Install

```sh
pip install "git+https://github.com/North9-Labs/north9.git"
```

**Requires Docker** for sandbox tools.

---

## Claude Code setup

```sh
# Core: sandbox + memory (17 tools)
python3 -m north9 --install

# Full suite: all 12 MCP servers registered at once
python3 -m north9 --suite
```

Restart Claude Code after either command.

### What `--install` wires up

**PreCompact hook** — fires before every `/compact` and auto-compact:

```
context fills → PreCompact hook fires
             → Claude saves memory before prose summary
             → memory_save() → .north9_state.json written to disk
             → /compact proceeds (brief prose is fine — memory has exact values)
             → next session: state reloads automatically
```

**SessionStart hook** — injects prior state before your first message:

```
[NORTH9] Prior session context restored:

OBJECTIVE: Fix authentication bug and deploy hotfix

COMPLETED (12 total, showing last 3):
  ✓ Reproduced failure → JWT decode fails when iss claim missing
  ✓ Located root cause → src/auth/middleware.py:87
  ✓ All tests passing → pytest tests/auth/ → 47 passed, 0 failed

FAILED — DO NOT RETRY:
  ✗ Direct deploy to prod → blocked by DEPLOY_POLICY_01 [DO NOT RETRY]

PENDING:
  → Merge PR #412 after CI green
  → Deploy to staging: ./deploy.sh staging v1.4.2-hotfix

KEY FACTS:
  • root_cause: src/auth/middleware.py:87
  • test_cmd: pytest tests/auth/ -v --tb=short
  • pr_url: https://github.com/acme/api/pull/412
```

Claude knows where it left off before you say a word.

---

## MCP servers

| Server | Start command | Tools | What it does |
|---|---|---|---|
| `north9` | `python -m north9` | 17 | Sandbox (Docker) + persistent memory |
| `north9-budget` | `python -m north9.budget` | 6 | Hard token/cost limits — block runaway spend |
| `north9-gate` | `python -m north9.gate` | 7 | PreToolUse policy enforcement — block dangerous calls |
| `north9-lens` | `python -m north9.lens` | 5 | Observability — trace every tool call, cost, latency |
| `north9-index` | `python -m north9.index` | 5 | Persistent keyword search across sessions |
| `north9-vault` | `python -m north9.vault` | 6 | Encrypted secrets — inject API keys at runtime |
| `north9-grid` | `python -m north9.grid` | 4 | Parallel execution — N tasks at 30s → still 30s |
| `north9-scout` | `python -m north9.scout` | 4 | Fetch any URL, full-text search stored content |
| `north9-forge` | `python -m north9.forge` | 5 | Eval framework — YAML test cases, catch regressions |
| `north9-sift` | `python -m north9.sift` | 4 | Load CSV/JSON into SQLite, query with SQL |
| `north9-chain` | `python -m north9.chain` | 4 | YAML workflow runner connecting all north9 tools |
| `north9-autopsy` | `python -m north9.autopsy` | 4 | Post-mortem — find waste patterns in agent sessions |
| `prism` CLI | `prism inspect/report/diff/fork` | — | Record, replay, diff, fork any agent session |

All servers register with `python -m north9 --suite`.

---

## Core tools: sandbox + memory

### Sandbox

Every command runs in an isolated Docker container. Your host is never touched.

| Tool | Description |
|---|---|
| `bash` | Run any command — apt-get, pip, pytest, curl |
| `write_file` | Write files to /workspace (visible live on host) |
| `read_file` | Read files |
| `list_files` | List workspace contents |
| `snapshot` | Checkpoint container state before installs or risky ops |
| `rollback` | Restore to a previous snapshot |
| `export_file` | Copy a file from the container to the host |
| `upload_file` | Copy a host file into the container |
| `sandbox_status` | Workspace path, image, network, snapshots |

### Memory

Structured state that survives every `/compact` and session restart.

| Tool | Description |
|---|---|
| `memory_get` | Read full state — call at session start |
| `memory_set_objective` | Set the session goal |
| `memory_mark_completed` | Record a step with exact result |
| `memory_mark_failed` | Record a dead end — never retried |
| `memory_add_pending` | Add a concrete next step |
| `memory_complete_pending` | Mark a pending item done |
| `memory_anchor` | Pin an exact value (path, URL, command, error) |
| `memory_save` | Checkpoint before risky operations |
| `memory_reset` | Clear all state |

---

## Sandbox image options

```sh
# Default: python:3.12-slim, no internet
python3 -m north9

# Ubuntu with internet — for apt-get, npm, general tools
python3 -m north9 --image ubuntu:22.04 --network bridge

# Pre-built runtime: Python + Node + git + gcc + ripgrep + pnpm
docker build -t north9-runtime . && python3 -m north9 --image north9-runtime --network bridge
```

---

## Memory benchmark

40-turn realistic debugging session (FastAPI JWT auth incident). 10 critical values tracked. Resumption quality measured by asking Claude 10 factual questions using only the compressed context.

```
                              Tokens    vs raw    Values    Resumption QA
─────────────────────────────────────────────────────────────────────────
Raw retention                  2,437      1.0x    10/10             —
Prose summary (/compact)         267      9.1x     5/10           7/10
north9 memory (structured)       584      4.2x     9/10           9/10
```

Prose is 2.2× smaller but answers 2 fewer questions correctly. The gap is larger on value presence (5/10 vs 9/10) — prose drops the exact test command, PR URL, and deploy command. An agent resuming from prose has to re-investigate; north9 has the exact values ready.

Run it yourself: `python3 benchmark/run.py` (uses `claude` CLI — Claude Code required).

---

## Python API

### Sandbox

```python
import north9

with north9.Sandbox(network="bridge") as env:
    env.run("pip install flask requests")
    env.snapshot("after-install")

    env.write_file("app.py", "from flask import Flask\napp = Flask(__name__)")
    result = env.run("python -c 'import flask; print(flask.__version__)'")

    if not result.success:
        env.rollback("after-install")

    env.export("output.json", "./output.json")
```

### Memory

```python
import anthropic, north9

client = anthropic.Anthropic()
mem = north9.Memory(client, keep_recent=10, token_limit=150_000)

while not done:
    mem.add("user", get_next_input())
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        messages=mem.messages,
    )
    mem.add("assistant", response.content[0].text)

mem.anchor("root_cause: src/auth/middleware.py:87")
```

### Combined

```python
import anthropic, north9

client = anthropic.Anthropic()
mem = north9.Memory(client, keep_recent=10, token_limit=150_000)

with north9.Sandbox(network="bridge") as env:
    while not done:
        mem.add("user", get_next_input())
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8096,
            tools=north9.TOOL_DEFINITIONS,
            messages=mem.messages,
        )
        for block in response.content:
            if block.type == "tool_use":
                result = env.handle_tool_call(block.name, block.input)
                mem.add_tool_use(block.name, block.input, tool_id=block.id)
                mem.add_tool_result(block.id, result)
        mem.add("assistant", response.content[0].text)
```

### Async

```python
import anthropic, north9

client = anthropic.AsyncAnthropic()
mem = north9.AsyncMemory(client, keep_recent=10, token_limit=150_000)

async with north9.AsyncSandbox(network="bridge") as env:
    while not done:
        mem.add("user", get_next_input())
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8096,
            messages=await mem.get_messages(),
        )
        mem.add("assistant", response.content[0].text)
```

---

## Configuration

### Sandbox

```python
north9.Sandbox(
    image="python:3.12-slim",  # Docker image — ubuntu:22.04 for apt+general tools
    workspace_dir=None,        # Host path for /workspace (auto-created)
    name=None,                 # Container name (auto-generated)
    network="none",            # "none" (isolated) | "bridge" (internet)
    memory="512m",             # Memory limit: "512m", "1g", "2g"
    cpus=1.0,                  # CPU limit
    pids_limit=512,            # Fork bomb protection
    ports=None,                # {host_port: container_port} for web servers
)
```

### Memory

```python
north9.Memory(
    client,
    keep_recent=10,                              # turns kept verbatim
    token_limit=150_000,                         # compression threshold
    compress_model="claude-haiku-4-5-20251001",  # compression model
    max_tool_result_chars=4_000,                 # truncate large tool outputs
    max_completed=100,                           # cap completed list size
    on_compress=callback,                        # called after each compression
    compress_prompt=custom_instructions,         # domain-specific compression
)
```

**Recommended limits:**

| Model | Context | `token_limit` |
|---|---|---|
| claude-sonnet-4-6 | 200k | 150,000 |
| claude-opus-4-8 | 200k | 150,000 |
| gpt-4o | 128k | 90,000 |

### Environment variables (MCP server)

| Variable | Default | Description |
|---|---|---|
| `NORTH9_IMAGE` | `python:3.12-slim` | Docker image |
| `NORTH9_NETWORK` | `none` | `none` or `bridge` |
| `NORTH9_MEMORY` | `512m` | Container memory limit |
| `NORTH9_CPUS` | `1.0` | Container CPU limit |
| `NORTH9_WORKSPACE` | `~/.north9/workspaces/default` | Host workspace path |
| `NORTH9_NAME` | `north9-default` | Container name |
| `NORTH9_STATE_FILE` | `.north9_state.json` | Memory state file |

---

## Security model

| Protection | Mechanism |
|---|---|
| Filesystem isolation | Container has its own root; only `/workspace` shared with host |
| Symlink escape prevention | `read_file`/`write_file` detect and block path escapes |
| No privilege escalation | `--security-opt no-new-privileges` |
| Minimal capabilities | `--cap-drop=ALL` + only `SETUID SETGID CHOWN FOWNER DAC_OVERRIDE` |
| Network isolation | `network="none"` by default; `bridge` opt-in; `host` blocked |
| Resource limits | `--pids-limit=512`, `--memory=512m`, `--cpus=1.0` |
| Workspace validation | Refuses system paths: `/etc`, `/usr`, `/var/run`, `/` |

---

## How memory compression works

```
Turns 1–N (older)                        Turns N-9 to N (recent)
────────────────────────────────────────────────────────────────
[user: run the test suite        ]        [user: CI status?    ]  ← kept verbatim
[asst: TOOL_USE bash pytest ...  ]        [asst: Tests passing ]
[user: TOOL_RESULT: 47 passed    ]        [user: Try the fix   ]  ← keep_recent=10
[user: check middleware.py       ]        [asst: TOOL_USE ...  ]
[asst: line 87 missing issuer    ]        [user: TOOL_RESULT . ]
...
        │
        ▼  sent to claude-haiku for compression
        ▼
[NORTH9: compressed prior context]  ← injected into system prompt
OBJECTIVE: Fix auth bug
COMPLETED: ✓ Found root cause → middleware.py:87
FAILED:    ✗ RS256 → KeyError: 'RS256_PRIVATE_KEY' [DO NOT RETRY]
KEY FACTS: • test_cmd: pytest tests/auth/ -v
```

State accumulates across compressions. Pending items clear automatically when marked completed (fuzzy prefix match).

---

## Comparison

| | Raw | `/compact` prose | north9 |
|---|---|---|---|
| Hits context limit | Eventually | No | No |
| Exact file paths preserved | Yes | ✗ lost | **Yes** |
| Exact error messages | Yes | ✗ paraphrased | **Yes** |
| "Do not retry" tracking | No | No | **Yes** |
| Safe execution | No | No | **Yes** |
| Snapshot + rollback | No | No | **Yes** |
| Cost enforcement | No | No | **Yes** |
| Policy/gate enforcement | No | No | **Yes** |
| Session recording + replay | No | No | **Yes** |
| Claude Code MCP | — | — | **Yes** |
| Auto session restore | — | — | **Yes** |

---

## License

MIT — [North9 Labs](https://north9.org)
