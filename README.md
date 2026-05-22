<div align="center">

# north9

**The missing runtime for autonomous AI agents.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://github.com/North9-Labs/north9/actions/workflows/ci.yml/badge.svg)](https://github.com/North9-Labs/north9/actions)

</div>

---

Two things kill long-running AI agents in production:

**They destroy things.** Agent runs `rm -rf`, installs malware, loops forever on the file system. You give an agent a `bash` tool and hope for the best.

**They forget everything.** Context window fills up. `/compact` turns 200 turns of exact file paths, error messages, and test results into vague prose. The agent starts over — retries the same broken approach, asks the same questions, loses the exact commands it already ran.

These aren't separate problems. An agent that can't safely execute anything can't do real work. An agent that loses its mind every 150k tokens can't do long work. north9 solves both.

```
Sandbox   every command runs in Docker — your machine is always safe
Memory    structured state survives /compact and session restarts
```

---

## Results

Simulated 200-turn coding session. 8 critical values tracked across a context reset: file paths, error messages, URLs, commands.

```
                              Tokens    vs raw    Values preserved
─────────────────────────────────────────────────────────────────
Raw retention                  7,102      1.0x    5/8
Prose summary (/compact)         454     15.6x    0/8   (0%)
north9 memory (structured)       668     10.6x    8/8  (100%)
```

Prose compresses harder but loses every critical value — the agent can't resume meaningful work. north9 trades ~200 extra tokens to preserve all 8. After any context reset, the agent knows exactly where it left off.

---

## Claude Code setup

One command:

```sh
pip install "git+https://github.com/North9-Labs/north9.git#egg=north9[mcp]" && python3 -m north9 --install
```

Restart Claude Code. **Requires Docker running.**

`--install` wires in three things:

**1. PreCompact hook** (`~/.claude/hooks/north9-pre-compact.py`)

Fires before *every* compaction — both auto-compact (context limit) and manual `/compact`. Claude sees instructions to save memory state before the prose summary is generated. Zero user action needed.

```
Context fills up
  → PreCompact hook fires
  → Claude calls memory_mark_completed / memory_mark_failed / memory_anchor
  → memory_save() — .north9_state.json written
  → /compact proceeds (brief prose is fine — memory has the exact values)
  → Next session: state reloads automatically
```

**2. SessionStart hook** (`~/.claude/hooks/north9-session-start.py`)

At every session start, Claude sees prior state injected before the first message:

```
[NORTH9] Prior session context restored:

OBJECTIVE: Fix authentication bug and deploy hotfix

COMPLETED (12 total, showing last 3):
  ✓ Reproduced failure locally → JWT decode fails when iss claim missing
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

**3. MCP server with 17 tools**

All sandbox execution and memory management available as MCP tools directly in Claude Code.

### MCP tools

**Sandbox — isolated Docker execution:**

| Tool | When to use |
|---|---|
| `bash` | Run any command — apt-get, pip, pytest, make, curl |
| `write_file` | Write files to /workspace (visible live on host) |
| `read_file` | Read files from the sandbox |
| `list_files` | List workspace contents |
| `snapshot` | Checkpoint container before installs or risky ops |
| `rollback` | Restore to snapshot if something breaks |
| `export_file` | Copy a file out of the container to the host |
| `upload_file` | Copy a host file into the container |
| `sandbox_status` | Show workspace path, image, snapshots |

**Memory — persists across compactions and restarts:**

| Tool | When to use |
|---|---|
| `memory_get` | Read full state — call at session start |
| `memory_set_objective` | Set the session goal |
| `memory_mark_completed` | After each step — include exact result |
| `memory_mark_failed` | Dead ends — include exact error, never retried |
| `memory_add_pending` | Concrete next steps |
| `memory_complete_pending` | Mark a pending item done |
| `memory_anchor` | Pin exact values: paths, URLs, commands, errors |
| `memory_save` | Checkpoint before risky operations |
| `memory_load` | Restore from a checkpoint file |
| `memory_reset` | Clear all state and start fresh |

### Sandbox options

```sh
# Default: python:3.12-slim, no internet
python3 -m north9

# Ubuntu with internet (for apt-get, npm, general tools)
python3 -m north9 --image ubuntu:22.04 --network bridge --memory 1g

# Use the full-featured runtime image (Python + Node + build tools)
docker build -t north9-runtime . && python3 -m north9 --image north9-runtime --network bridge
```

The included `Dockerfile` builds a `north9-runtime` image with Python, Node.js, git, gcc, ripgrep, jq, pnpm, and build tools pre-installed.

---

## Python API

### Sandbox

```python
import north9

# Context manager — container starts on enter, destroyed on exit
with north9.Sandbox(network="bridge") as env:
    # Install what you need
    env.run("pip install flask requests")
    env.snapshot("after-install")

    # Write files
    env.write_file("app.py", """
from flask import Flask
app = Flask(__name__)

@app.route("/health")
def health():
    return {"status": "ok"}
""")

    # Run it
    result = env.run("python app.py &")

    # Rollback if anything breaks
    if not result.success:
        env.rollback("after-install")

    # Pull the output to your machine
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

    # mem.messages auto-compresses when over token_limit
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8096,
        messages=mem.messages,
    )

    mem.add("assistant", response.content[0].text)

# Pin facts that must survive every compression
mem.anchor("root_cause: src/auth/middleware.py:87")
mem.anchor("test_cmd: pytest tests/auth/ -v --tb=short")
```

### Combined — the full loop

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
    workspace_dir=None,        # Host path for /workspace (auto-created if None)
    name=None,                 # Container name (auto-generated if None)
    network="none",            # "none" (isolated) | "bridge" (internet)
    memory="512m",             # Memory limit: "512m", "1g", "2g"
    cpus=1.0,                  # CPU limit
    pids_limit=512,            # Fork bomb protection
    ports=None,                # {host_port: container_port}
)
```

### Memory

```python
north9.Memory(
    client,
    keep_recent=10,                              # turns kept verbatim
    token_limit=150_000,                         # compression threshold
    compress_model="claude-haiku-4-5-20251001",  # model for compression
    max_tool_result_chars=4_000,                 # truncate large tool outputs
    max_completed=100,                           # cap completed list size
    on_compress=callback,                        # called after each compression
    compress_prompt=custom_instructions,         # override default compression prompt
)
```

**Recommended `token_limit`:**

| Model | Context | Recommended limit |
|---|---|---|
| claude-opus-4-7 | 200k | 150,000 |
| claude-sonnet-4-6 | 200k | 150,000 |
| gpt-4o | 128k | 90,000 |

---

## Security model

| Protection | Mechanism |
|---|---|
| Filesystem isolation | Container has own root; only `/workspace` shared with host |
| Symlink escape prevention | `read_file`/`write_file` detect and block escapes |
| No privilege escalation | `--security-opt no-new-privileges` |
| Minimal capabilities | `--cap-drop=ALL`, adds back only `SETUID SETGID CHOWN FOWNER DAC_OVERRIDE` |
| Network isolation | `network="none"` by default; `bridge` opt-in; `host` blocked |
| Resource limits | `--pids-limit=512`, `--memory=512m`, `--cpus=1.0` |
| Workspace validation | Refuses to mount system paths (`/etc`, `/usr`, `/var/run`, `/`) |

---

## How compression works

```
Turns 1–N (older)                          Turns N-9 to N (recent)
──────────────────────────────────────────────────────────────────
[user: run the test suite         ]         [user: CI status?     ]  ← kept verbatim
[asst: TOOL_USE bash pytest ...   ]         [asst: Tests passing  ]
[user: TOOL_RESULT: 47 passed     ]         [user: Try the fix    ]  ← keep_recent=10
[user: check middleware.py        ]         [asst: TOOL_USE ...   ]
[asst: line 87 missing issuer     ]         [user: TOOL_RESULT .. ]
...
        │
        ▼  sent to claude-haiku for compression
        │
        ▼
[NORTH9: compressed prior context]  ← merged into system prompt
OBJECTIVE: Fix auth bug
COMPLETED: ✓ Found root cause → middleware.py:87
FAILED:    ✗ RS256 → KeyError: 'RS256_PRIVATE_KEY' [DO NOT RETRY]
KEY FACTS: • test_cmd: pytest tests/auth/ -v
```

State accumulates across compressions. Pending items clear automatically when marked completed (fuzzy prefix match).

---

## Custom compression prompt

For domain-specific agents, override what gets preserved:

```python
DEVOPS_PROMPT = """
You are compressing AI agent history for a DevOps agent.
Preserve exactly: server hostnames, IPs, ports, deployment commands,
service versions, exit codes, error messages, alert thresholds.
Output the standard YAML structure.
"""

mem = north9.Memory(client, compress_prompt=DEVOPS_PROMPT, ...)
```

---

## Comparison

| | Raw | `/compact` prose | north9 |
|---|---|---|---|
| Hits context limit | Eventually | No | No |
| Exact file paths preserved | Yes | ✗ lost | Yes |
| Exact error messages | Yes | ✗ paraphrased | Yes |
| "Do not retry" tracking | No | No | **Yes** |
| Safe execution | No | No | **Yes** |
| Rollback on failure | No | No | **Yes** |
| Drop-in, no refactor | — | No | **Yes** |
| Zero dependencies | — | — | **Yes** |
| Claude Code MCP | — | — | **Yes** |
| Auto session restore | — | — | **Yes** |

---

## License

MIT — [North9 Labs](https://north9.io)
