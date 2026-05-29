## north9

Complete runtime for autonomous AI agents. All tools live in one package.

Register everything: `python3 -m north9 --suite`

---

### Sandbox (north9) — Docker isolation

```
bash("command")                    # run anything in container
write_file("path", "content")      # write to /workspace
read_file("path")                  # read files
snapshot("before-install")         # checkpoint container state
rollback("before-install")         # undo if something breaks
sandbox_status()                   # workspace path + container info
```

Open the workspace path in your editor — files appear live.

### Memory (north9) — persists across compactions and restarts

```
memory_mark_completed("action → exact result, file, output")
memory_mark_failed("what failed → exact error")   # never retry these
memory_add_pending("concrete next step")
memory_anchor("key: exact value")                 # paths, URLs, commands, errors
memory_save()                                     # checkpoint before risky ops
memory_get()                                      # read full state
```

### Rules

- `snapshot()` before apt-get, pip install, or any system-level change
- `memory_mark_failed()` on every dead end — exact error, not vague
- `memory_anchor()` for any value that must survive context compression
- `memory_save()` before destructive operations
- Never retry items in the FAILED list

---

### Budget (north9-budget) — cost enforcement

```
budget_status()                           # tokens used, cost so far
budget_record(tokens_in, tokens_out, model)  # record a call
budget_set_limit(tokens, cost_usd)        # set hard limits
budget_reset()                            # clear session usage
```

### Gate (north9-gate) — policy enforcement

```
gate_status()                             # active rules
gate_check("tool_name", '{"arg": "val"}') # test a call against policy
gate_add_rule(name, tool, pattern, action, reason)
gate_remove_rule(name)
gate_reload()                             # reload policy from disk
```

Policy file: `~/.gate/policy.yaml`

### Lens (north9-lens) — observability

```
lens_record(tool_name, input, output, tokens_in, tokens_out, latency_ms)
lens_query(tool_name, limit)              # recent traces
lens_sessions(limit)                      # session summaries
lens_session_id()                         # current session ID
```

DB: `~/.lens/traces.db`

### Index (north9-index) — persistent keyword memory

```
index_add(content, tags, source)          # store a document
index_search(query, limit)                # keyword search (BM25/FTS5)
index_list(limit)                         # recent documents
index_delete(doc_id)                      # remove a document
```

DB: `~/.index/memory.db`

### Vault (north9-vault) — encrypted secrets

```
vault_set("KEY", "value", tags)           # store encrypted secret
vault_get("KEY")                          # retrieve
vault_list(tag)                           # list secrets (names only)
vault_delete("KEY")
vault_env("KEY1,KEY2")                    # return as shell export commands
```

Requires: `export NORTH9_VAULT_KEY='your-master-key'`

### Grid (north9-grid) — parallel execution

```
grid_run(tasks, max_workers)              # run N tasks concurrently
grid_map(prompt_template, items)          # map a prompt over a list
grid_status()                             # active worker info
```

### Scout (north9-scout) — web fetch + search

```
scout_fetch("https://...")               # fetch and store page
scout_search("query")                    # keyword search stored pages
scout_list()                             # all fetched pages
scout_delete("https://...")
```

`scout_fetch` is idempotent — re-fetching returns cached result (use `force=True` to refresh).

### Forge (north9-forge) — eval framework

```
forge_run("path/to/suite.yaml")          # run test suite
forge_list()                             # list available suites
forge_result(run_id)                     # get results for a run
```

YAML test format: `prompt`, `expected` assertions, `model` override.

### Sift (north9-sift) — SQL over CSV/JSON

```
sift_load("path/to/file.csv", table_name)  # load file into SQLite
sift_query("SELECT * FROM table LIMIT 10") # run SQL
sift_tables()                               # list loaded tables
sift_schema(table_name)                     # show columns and types
```

### Chain (north9-chain) — YAML workflows

```
chain_run("path/to/workflow.yaml")        # run a workflow
chain_list_tools()                        # all available tool names
chain_validate("path/to/workflow.yaml")   # validate without running
chain_status()                            # last run status
```

### Autopsy (north9-autopsy) — session behavioral analysis

```
autopsy_session("path/to/session.prism")  # find waste in a Prism session
autopsy_lens(session_id)                  # analyze Lens trace records
autopsy_json("path/to/session.prism")     # findings as JSON
autopsy_compare("before.prism", "after.prism")  # regression comparison
```

Detects: dead loops, always-failing tools, redundant reads, ignored LLM output, token hogs.

### Prism (CLI) — session recording + replay

```bash
prism inspect session.prism      # summary + frame list
prism report session.prism       # generate HTML visualization
prism diff a.prism b.prism       # diff two sessions
prism fork session.prism --at 5  # describe a fork plan
```

Use `Recorder` in Python to capture sessions:

```python
from north9.prism.capture import record

with record("session.prism") as rec:
    client = rec.wrap_anthropic(anthropic.Anthropic())
    # all API calls now captured
```
