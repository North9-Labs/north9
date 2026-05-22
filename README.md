<div align="center">

# north9

**The missing runtime for autonomous AI agents.**

Run anything. Forget nothing.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/North9-Labs/north9/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/North9-Labs/north9/actions)

</div>

---

Two things kill long-running AI agents:

**They destroy things.** Give an agent a `bash` tool and it `rm -rf`s the wrong directory, fills your disk, or loops forever consuming CPU. There's no wall between the agent and your machine.

**They forget everything.** Context fills up. `/compact` turns 200 turns of exact file paths, error messages, and test results into vague prose. The agent loses where it was, retries the same broken approach, and asks questions it already answered three sessions ago.

north9 solves both in one package.

```
sandbox   every command runs in Docker — your machine is always safe
memory    structured state survives /compact and session restarts
```

---

## Results

Simulated 200-turn coding session. 8 critical values tracked across a context reset.

```
                              Tokens    vs raw    Values preserved
─────────────────────────────────────────────────────────────────
Raw retention                  7,102      1.0x    5/8
Prose summary (/compact)         454     15.6x    0/8   (0%)
north9 memory (structured)       668     10.6x    8/8  (100%)
```

Prose compresses 15× harder but loses every critical value — the agent can't resume. north9 trades ~200 extra tokens to preserve all 8. After any context reset, the agent picks up exactly where it left off.

---

## Claude Code setup

```sh
pip install "git+https://github.com/North9-Labs/north9.git#egg=north9[mcp]" && python3 -m north9 --install
```

Restart Claude Code. **Requires Docker.**

One command installs three things:

### 1 — PreCompact hook

Fires before **every** compaction — auto-compact when context fills and manual `/compact`. Claude saves structured memory before the prose summary is generated. No user action required.

```
context fills → PreCompact hook fires
             → memory_mark_completed / memory_mark_failed / memory_anchor
             → memory_save()  →  .north9_state.json written to disk
             → /compact proceeds (brief prose is fine — memory has exact values)
             → next session: state reloads automatically
```

### 2 — SessionStart hook

Prior state is injected before the first message of every session:

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

Sandbox ready — use bash(), write_file(), snapshot().
```

Claude knows exactly where it left off before you say a word.

### 3 — MCP server (17 tools)

**Sandbox** — isolated Docker execution:

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

**Memory** — persists across context resets:

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

### Sandbox image options

```sh
# Default: python:3.12-slim, no internet
python3 -m north9

# Ubuntu with internet — for apt-get, npm, general tools
python3 -m north9 --image ubuntu:22.04 --network bridge

# Pre-built runtime: Python + Node + git + gcc + ripgrep + pnpm
docker build -t north9-runtime . && python3 -m north9 --image north9-runtime --network bridge
```

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

The agent can `rm -rf /workspace`. Your machine is fine.

### Memory

```python
import anthropic, north9

client = anthropic.Anthropic()
mem = north9.Memory(client, keep_recent=10, token_limit=150_000)

while not done:
    mem.add("user", get_next_input())

    # mem.messages auto-compresses when over token_limit
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8096,
        messages=mem.messages,
    )

    mem.add("assistant", response.content[0].text)

# Pin facts that survive every compression
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
            model="claude-opus-4-7",
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
| claude-opus-4-7 | 200k | 150,000 |
| claude-sonnet-4-6 | 200k | 150,000 |
| gpt-4o | 128k | 90,000 |

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
| Zero required dependencies | — | — | **Yes** |
| Claude Code MCP | — | — | **Yes** |
| Auto session restore | — | — | **Yes** |

---

## North9 Labs

north9 is part of a suite of tools for building serious AI agents:

| Project | What it does |
|---|---|
| **north9** ← you are here | Sandbox + memory — safe execution and persistent context |
| [**Prism**](https://github.com/North9-Labs/Prism) | Time-travel debugger — record, replay, and fork any agent session |
| [**Seam**](https://github.com/North9-Labs/Seam) | Post-quantum encrypted communications over UDP |

---

## License

MIT — [North9 Labs](https://north9.io)
