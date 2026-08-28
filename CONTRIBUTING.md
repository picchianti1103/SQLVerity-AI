# Contributing to SQLVerity AI

Thank you for helping improve governed natural-language-to-SQL workflows. SQLVerity AI is a
developer preview, so contributions should preserve its fail-closed security and governance
boundaries while keeping behavior testable and explicit.

## Development setup

SQLVerity AI requires Python 3.12 or newer.

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

Activate the virtual environment using the command appropriate for your shell. Install only the
database integrations you need, for example:

```bash
python -m pip install -e ".[dev,postgres]"
```

Available extras are `postgres`, `mysql`, `oracle`, `sqlserver`, `openai`, and `all`. MariaDB
connector support remains in the codebase, but its installation extra is temporarily withheld until
the upstream Python distribution has a release that resolves `PYSEC-2026-217`. Claude, Gemini, Kimi,
and Ollama use the core HTTP client and do not require provider SDK extras.

## Verification gate

Run all checks before opening a pull request:

```bash
python -m ruff check apps packages tests
python -m mypy apps packages tests --no-incremental
python -m pytest -q -p no:cacheprovider
python -m pip_audit .
python -m packages.evaluation.sqlverity_evaluation.cli \
  --dataset fixtures/questions/golden_v1.json \
  --thresholds fixtures/questions/golden_thresholds_v1.json \
  --baseline fixtures/questions/golden_baseline_v1.json
```

## Design expectations

- Parse and transform SQL through the dialect-aware AST boundary; do not introduce string-based
  validation or interpolation of external values.
- Keep database execution read-only, bounded, cancellable, and tied to the catalog version that was
  validated.
- Never persist credentials, raw parameter values, prompts, result rows, or sensitive answer text.
- Keep tenant and DataSource authorization explicit on every new API path.
- Treat inferred semantics as reviewable evidence, never as confirmed truth.
- Add an ADR under `docs/adr` for a new architectural boundary or a material contract change.
- Add focused tests and update the golden dataset only through its reviewed curation workflow.

## Pull requests

Keep changes scoped and describe their safety, privacy, migration, and compatibility impact. A pull
request must pass CI and should not combine unrelated refactoring with functional changes. Report
security vulnerabilities through the private process described in `SECURITY.md`, not in a public
issue.

## Licensing

SQLVerity AI is licensed under Apache-2.0. Unless you explicitly state otherwise, any contribution you
intentionally submit for inclusion in SQLVerity AI is provided under the same license, without additional
terms or conditions.
