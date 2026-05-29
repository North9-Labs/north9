# Changelog

## [0.2.0] — 2026-05-29

Monorepo release. All North9 tools consolidated into a single package.

### New tools

- **Budget** (`north9-budget`) — hard token/cost limits, block runaway agent spend
- **Gate** (`north9-gate`) — PreToolUse policy enforcement via YAML rules
- **Lens** (`north9-lens`) — observability tracer, records every tool call with cost + latency
- **Index** (`north9-index`) — persistent keyword search across sessions (SQLite FTS5/BM25)
- **Vault** (`north9-vault`) — encrypted secrets store, inject API keys at runtime
- **Grid** (`north9-grid`) — parallel execution, N tasks at 30s each → still 30s total
- **Scout** (`north9-scout`) — fetch any URL, full-text search stored content
- **Forge** (`north9-forge`) — YAML eval framework, run test suites, catch regressions
- **Sift** (`north9-sift`) — load CSV/JSON into SQLite, query with SQL
- **Chain** (`north9-chain`) — YAML workflow runner connecting all north9 tools
- **Prism** — session recorder + time-travel debugger, record/replay/fork/diff agent sessions
- **Autopsy** (`north9-autopsy`) — post-mortem analysis: detect dead loops, wasted tokens, failing tools

### Breaking changes

- Package version bumped to `0.2.0`
- Suite install (`--suite`) now registers all MCP servers from the monorepo — no external pip installs
- MCP server keys renamed: `"gate"` → `"north9-gate"`, `"budget"` → `"north9-budget"`, etc.

### Improvements

- Single `pip install north9` covers everything
- `python -m north9 --suite` registers all 12 MCP servers in one command
- `python -m north9 --status` shows registration status of all servers
- 540 tests covering all modules

---

## [0.1.0] — 2026-05-22

Initial release. Merges Cage (sandbox) and Scroll (memory) into a single package.

### Sandbox
- Docker-isolated execution — every command in an ephemeral container
- `/workspace` volume-mounted from host — files visible live in your editor
- `snapshot()` / `rollback()` — checkpoint and restore container state
- `Sandbox`, `AsyncSandbox`, `ExecResult`
- Security: `--cap-drop=ALL`, `--security-opt no-new-privileges`, pids limit, workspace validation

### Memory
- Structured state: objective / completed / failed / pending / facts
- Survives every `/compact` and session restart
- `Memory`, `AsyncMemory`, `MemoryState`
- `anchor(fact)` — pin exact values that survive all compressions

### MCP server
- 17 tools: 8 sandbox + 9 memory
- `python3 -m north9 --install` — wires everything into Claude Code
- `PreCompact` hook — saves memory before every `/compact`
- `SessionStart` hook — auto-injects prior state at every session start
