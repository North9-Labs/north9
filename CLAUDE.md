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
