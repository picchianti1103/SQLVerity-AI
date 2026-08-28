# Code audit, refactoring, and roadmap reconciliation

- **Date:** 2026-08-16
- **Scope:** all application, package, test, migration, fixture, packaging, and project-documentation files
- **Functional increment count:** unchanged at 17; this work is a maintenance pass
- **Source roadmap:** internal platform roadmap, version 1.0 (not included in the public repository)

> Post-audit note: Git was initialized and this audited 17-increment tree was recorded as the first
> baseline commit. Increment 18 (the OpenAI cloud adapter) was then implemented separately; current
> counts and blockers live in
> `docs/implementation-status.md`.

The workspace does not contain a Git repository. The audit therefore used full-tree structural,
static, behavioral, and artifact comparisons; no commit-based diff or rollback baseline was
available.

## Executive result

The audited code has no broken package exports, detected inter-package dependency cycles,
unreferenced private top-level definitions, or non-trivial exact function clones. The production
code no longer relies on `assert`, contains no type-ignore directives, passes strict typing and the
configured lint gate, and retains the full functional and golden regression behavior.

The main runtime improvement removes catalog and access-list N+1 query patterns. Catalog consumers
now load columns once per tenant/catalog version and semantic resolutions once per DataSource.
Security principal summaries and golden-candidate listings likewise use fixed-count aggregate reads.

## Delivered increments recap

| # | Increment | Delivered outcome |
|---:|---|---|
| 1 | Foundation | Domain entities, explicit query state machine, tenant catalog, audit, SQLite adapter, PostgreSQL baseline migration, and minimal API |
| 2 | PostgreSQL acquisition | Secret-referenced PostgreSQL connector and schema/PK/FK/view introspection |
| 3 | Offline acquisition | Governed DDL/manual import and minimal Schema Explorer |
| 4 | Semantic governance | Immutable evidence, epistemic precedence, review/history, correction, and optimistic concurrency |
| 5 | LLM boundary | Provider-neutral gateway, classified prompt egress, structured inference contracts, and usage events |
| 6 | Context and generation | Deterministic context retrieval, graph expansion, inspectable context, and structured SQL generation |
| 7 | SQL safety | PostgreSQL AST parsing, SELECT-only policy, lineage/catalog/function/wildcard checks, and bounded preview SQL |
| 8 | Query lifecycle and execution | Persisted approval lifecycle, catalog binding, EXPLAIN, read-only execution, timeout, bounds, and cancellation |
| 9 | Results | Deterministic local result summaries, privacy enforcement, redaction, and full provenance |
| 10 | FinOps | Effective-dated pricing, usage/cost calculation, budgets, summaries, and database-cost governance |
| 11 | Authorized Query DataSource | Versioned virtual query surfaces, declared schema/parameters, AST wrapping, bound values, and lifecycle binding integrity |
| 12 | Golden evaluation | Versioned 50-case dataset, production-validator runner, strict thresholds, hash-bound baseline, and regression gate |
| 13 | Corrected SQL learning | Immutable human corrections, revision chains, AST revalidation, drift filtering, deterministic retrieval, and prompt policy |
| 14 | Business concepts | Governed concepts/synonyms, conflicts, history/correction, normalized matching, context seeding, and classification propagation |
| 15 | Metrics and rules | Governed metric/rule evidence, AST fragment validation, dependencies, classification, generation enforcement, and provenance |
| 16 | Feedback and curation | Final feedback, real rates, golden-candidate eligibility, immutable review, and approved-only export |
| 17 | Minimum authentication/RBAC | Bearer authentication, bootstrap authority, tenant/DataSource roles, permissions, hashed/expiring/revocable keys, and trusted actors |

## Roadmap reconciliation

The previous status overstated completion of the document's concrete sequence and understated later
phase progress. The corrected accounting is:

- **Concrete sequence:** 19 of 20 entries complete. Entry 7 is partial because the LLM Gateway is
  complete but the required concrete provider is not.
- **Explicit MVP scope:** 17 of 19 included capabilities complete. A concrete cloud provider and a
  concrete local-LLM adapter remain missing.
- **Phase 8:** in progress, with the governed learning core already implemented.
- **Phase 9:** in progress, because Authorized Query DataSources are implemented; second-dialect and
  hybrid/vector retrieval work remains.
- **Phase 10:** in progress at its minimum boundary; opaque-key authentication/RBAC exists, while
  SSO/MFA/group federation and private-deployment hardening remain.

The authoritative detailed blocker and deferred-item list remains in
`docs/implementation-status.md`.

## Refactoring and optimization applied

### Catalog reads

Added one tenant/catalog-version-scoped column read and reused it in Schema Explorer, semantic
governance, concept/metric validation, semantic inference, corrected-SQL validation, golden
eligibility, Context Builder, and result-classification reload.

Context Builder also loads confirmed semantic resolutions once. Its previous database behavior grew
with every schema object and column, including repeated semantic lookups for selected columns. The
new path performs one column query and one semantic-resolution query regardless of catalog width.

### Security and curation reads

- Principal listing changed from `1 + 3P + C` reads (`P` principals, `C` credentials) to five
  aggregate reads: principals, tenant roles, DataSource roles, credentials, and revocations.
- Golden-candidate listing changed from one review read per candidate to one candidate read plus one
  joined review read.
- Query-count regression tests now enforce these aggregate access patterns.

### Duplicate and defensive code

- Moved the exact duplicate case/accent-insensitive Unicode term normalizer into one domain helper.
- Reused one column row mapper for single-object and catalog-version reads.
- Removed all production `assert` statements and replaced them with explicit fail-closed validation
  or invariant errors that also run under `python -O`.
- Replaced the OpenAPI method assignment/type-ignore with a typed `FastAPI` subclass.
- Sorted public exports and removed the catalog package's import-after-`__all__` ordering anomaly.
- Replaced small append loops with direct bulk extension and simplified nested context managers.

## Static and architectural findings

| Check | Result |
|---|---:|
| Strictly typed source files checked | 93 |
| Functions inspected in `apps` and `packages` | 550 |
| Non-trivial exact function-clone groups | 0 |
| Detected inter-package dependency cycles | 0 |
| Broken `__all__` exports across package initializers | 0 |
| Unreferenced private top-level definitions | 0 |
| Production `assert` statements | 0 |
| Production `type: ignore` directives | 0 |
| Broken installed requirements (`pip check`) | 0 |

An extended, non-default lint pass retains 27 reviewed findings:

- 17 complexity warnings in validation/parsing/orchestration functions;
- 6 SQL-construction warnings where only internal constant clauses and generated `?` placeholders
  are interpolated, while every external value remains parameter-bound;
- 4 intentionally unused parameters required by the `PolicyEngine` protocol implementation.

These are not active correctness or injection defects. The highest-complexity orchestration points
remain maintenance risks: Context Builder, SQL validation/reference resolution, LLM gateway, and
structured proposal parsing. Splitting them further should be contract-driven and covered by new
behavior tests, not performed as mechanical file fragmentation.

## Material intentionally retained

- The SQLite schema and PostgreSQL migrations express the same model for different adapters; they
  are not disposable duplicates.
- `repository.py` is large, but it owns one transactional SQLite persistence boundary. A future split
  should follow bounded repository interfaces and transaction ownership.
- `apps/api/main.py` is large. Router extraction remains useful, but it should be a dedicated
  no-contract-change maintenance slice because lifespan wiring and permission dependencies are
  shared across all routes.
- Empty exception-class bodies are intentional typed error markers, not dead implementations.
- `.venv` is generated but retained because it is the reproducible local test/runtime environment.

## Generated material removed

The cleanup removes only reproducible material ignored by source control: `build`, `dist`,
`sqlverity_platform.egg-info`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.docx_qa`, and all source
or test `__pycache__` directories. The wheel was rebuilt successfully in an external temporary
directory before deleting the workspace copy. `*.egg-info/` is now explicitly ignored.

## Verification gate

- Configured Ruff gate: passed.
- Strict mypy gate: passed on 93 source files.
- Pytest: 151 tests and 21 subtests passed.
- Golden dataset: 50/50 cases passed with zero regressions.
- Wheel build: passed using the installed build environment without downloading dependencies.
- Live PostgreSQL/provider tests: still blocked by the external conditions recorded in the status
  document.
