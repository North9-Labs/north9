## What

<!-- What changed and why — one paragraph max -->

## Checklist

- [ ] `python -m pytest tests/ --ignore=tests/test_integration.py --ignore=tests/test_security.py` passes (540 tests)
- [ ] `ruff check src/ tests/` passes
- [ ] `mypy src/north9/ --ignore-missing-imports` passes
- [ ] Integration tests if touching sandbox: `pytest tests/test_integration.py tests/test_security.py -m integration`

## Notes

<!-- Breaking changes, migration needed, anything reviewers should know -->
