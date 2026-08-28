# Code audit, hardening, and roadmap reconciliation

- **Date:** 2026-08-22
- **Scope:** application/API lifecycle, catalog persistence, quotas, workers, providers, Compose,
  load tooling, tests, packaging, CI, and project documentation
- **Functional increment count:** unchanged at 22; this is a corrective maintenance pass

## Executive result

The audit found four correctness or security-relevant gaps and several smaller lifecycle and
duplication issues. They are fixed with regression coverage. No claim of production certification is
made: real database/provider execution, hosted release controls, and deployment RPO/RTO still require
external systems and retained evidence.

## Corrected findings

| Priority | Finding | Resolution |
|---|---|---|
| High | Compose reused the privileged application-catalog database identity for the synthetic query demo. | The demo now has its own database, password, and NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOINHERIT login with only CONNECT, schema USAGE, and table SELECT. |
| High | A process crash could leave a shared concurrency counter occupied indefinitely. | A new request window resets orphaned concurrency; every lease carries its window and a late old-window release cannot decrement current work. |
| High | A completed semantic batch and its separately enqueued continuation could be split by a crash, losing the remaining cursor work. | Completion, audit evidence, and continuation enqueue now share one catalog transaction. |
| Medium | The load gate treated some non-2xx responses as successful and accepted health-like path prefixes. | Transport failures and every non-2xx response other than separately classified `429` throttling count as errors; allowed paths are exact. |
| Medium | Long-lived OIDC, provider, Vault, and AWS clients lacked a coordinated shutdown path. | Application shutdown now uses a cleanup stack and all supported resources expose or receive a close hook. |

## Refactoring and optimization

- Catalog backend selection, pool bounds, SQLite path handling, and secret resolution moved to one
  shared factory used by both the API and retention tooling.
- Provider adapters and the gateway use one close-if-supported helper instead of repeating reflective
  shutdown code.
- Redundant exception branches already covered by `ValueError` were removed.
- The demo fixture uses stable IDs, idempotent inserts, repaired identity sequences, and explicit
  default privileges so repeated setup remains deterministic.
- Background continuation avoids an additional repository transaction and eliminates a failure
  window rather than adding recovery polling.

## Material intentionally retained

- SQLite and PostgreSQL persistence definitions are separate adapters, not removable duplication.
- `apps/api/main.py` and the catalog repository remain large orchestration boundaries. Mechanical
  router/repository splitting would move code without reducing transaction or authorization
  complexity; future extraction should follow explicit bounded interfaces and behavior tests.
- Result rows remain local and deterministic. Optional narrative interpretation is deferred until it
  has a separate result-egress and privacy contract.
- Provider/database mocks and injected connections remain valuable offline contract tests, but do not
  replace live certification.

## Residual increments and external gates

The highest-value product work still deferred is hybrid/vector retrieval, stateful multi-turn
clarification, output-column lineage with selective masking, governed parameters in general generated
SQL, and FinOps provider/model comparison. Cross-DataSource federation and non-PostgreSQL Authorized
Query surfaces require separate policy and dialect contracts.

Before a general production claim, run the committed live matrix with approved credentials, preserve
execution accuracy/latency/cost/load reports, validate centralized telemetry and incident routing,
measure backup RPO/RTO, and complete the target deployment threat model. Before a public release,
configure required GitHub checks and branch protection, review ownership, and publish reviewed signed
artifacts with an SBOM. The authoritative status is
[implementation-status.md](implementation-status.md).

## Verification

- Ruff: all configured checks passed.
- Strict mypy: 159 source files passed.
- Pytest: 277 tests passed, 3 opt-in live tests skipped, and 50 subtests passed.
- The 50-case golden gate, distribution build/Twine validation, Compose interpolation, fixture
  checks, and Git diff integrity are part of the same release gate.
