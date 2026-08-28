# Priority increment audit: governed parameters and selective masking

- **Date:** 2026-08-24
- **Functional increment count:** 24
- **Scope:** proposal schema, AST validation, query tickets, execution adapters, result privacy,
  Query Studio, migrations, tests, and documentation

## Result

The two highest-priority self-contained roadmap gaps are implemented. General generated SQL can use
typed named parameters without persisting their values, and result masking now follows validated
per-output-column lineage. Unsafe or incomplete conditions continue to fail closed.

## Security and correctness controls

- Exact declaration/placeholder matching, safe identifiers, a 50-parameter bound, scalar type and
  finite-number checks, and static LIMIT/OFFSET enforcement.
- The same value signature is required for EXPLAIN, human approval, and execution; audit events omit
  both raw values and the signature.
- Native parameter binding is used for every direct database dialect; no value is interpolated into
  SQL text.
- Selective masking is enabled only when every runtime output matches complete AST lineage. Missing
  classifications or unresolved lineage retain highly-sensitive/whole-result behavior.
- SQLite compatibility upgrades and PostgreSQL migration `0015` persist only declarations and
  lineage metadata.

## Remaining priority increments

The next product priorities are stateful multi-turn clarification and saved/shareable workspaces,
hybrid/vector retrieval with deterministic reranking, and FinOps provider/model simulation. Live
database/provider certification, hosted branch protections, and retained production RPO/RTO evidence
remain external release gates rather than offline code increments.

## Verification

- Ruff: all configured checks passed.
- Strict mypy: 160 source files passed.
- Pytest: 284 tests passed, 3 opt-in live tests skipped, and 50 subtests passed.
- Golden gate: 50 of 50 cases passed with zero regressions.
- Local sdist/wheel build and Twine metadata validation passed; hosted CI retains the isolated build.
- The real FastAPI `/ui` path loaded the Query Studio HTML, stylesheet, and JavaScript without
  browser console errors, including the new parameter controls.
