# Contributing to north9

## Setup

```bash
git clone https://github.com/North9-Labs/north9.git
cd north9
pip install -e ".[dev]"
```

Docker required for integration tests. Unit tests run without it.

## Project structure

- `src/north9/sandbox/` — Docker execution layer
- `src/north9/memory/` — persistent context layer
- `src/north9/mcp.py` — Claude Code integration

## Running tests

Unit tests (no Docker needed):
```bash
pytest tests/test_memory.py tests/test_sandbox.py
```

Integration tests (Docker required):
```bash
pytest tests/test_integration.py tests/test_security.py -m integration
```

## Lint and type check

```bash
ruff check src/ tests/
mypy src/north9/ --ignore-missing-imports
```

## Style

- Enforced by ruff
- 100 character line length
- Python 3.10+

## PRs

- Unit tests required for all changes
- Integration tests required for any sandbox changes
- No breaking API changes without opening an issue for discussion first
- Keep commits focused; squash fixups before requesting review
