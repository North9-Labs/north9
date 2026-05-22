# Changelog

## [0.1.0] — 2026-05-22

Initial release. Merges cage-agent (sandbox) and scroll-agent (memory) into a single package.

### Sandbox
- Docker-isolated execution environment — every command in an ephemeral container
- `/workspace` volume-mounted from host — files visible live in your editor
- `snapshot()` / `rollback()` — checkpoint and restore container state
- `Sandbox`, `AsyncSandbox`, `ExecResult`, `StreamResult`
- RTK integration — compresses command output before returning to model
- Security: `--cap-drop=ALL`, `--security-opt no-new-privileges`, pids limit, workspace validation

### Memory
- Structured YAML state: objective / completed / failed / pending / facts
- Survives every `/compact` and session restart
- `Memory`, `AsyncMemory`, `MemoryState`
- `anchor(fact)` — pin exact values that survive all compressions
- `max_completed` — cap completed list size (default 100)
- `window_tokens` in stats — accurate token estimate including state + pinned messages
- `preview_compress_input()` — debug what the compression model receives

### MCP server (Claude Code)
- 17 tools: 8 sandbox + 9 memory
- `python3 -m north9 --install` — one command wires everything into Claude Code
- `PreCompact` hook — fires before every `/compact` and auto-compact; Claude saves memory state automatically
- `SessionStart` hook — auto-injects prior state at every session start
- `/project:compact`, `/project:save`, `/project:restore` custom commands
- `CLAUDE.md` instructions appended automatically
