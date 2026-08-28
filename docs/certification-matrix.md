# Release certification matrix

This matrix separates implemented adapters from combinations that may be advertised as supported.
`Pending live` means offline contracts and an executable harness exist, but no retained real-service
evidence has been produced. A release row becomes `Certified` only after the linked workflow report
is reviewed and retained with the release artifacts.

## Initial supported perimeter

| Surface | Candidate status | Offline gate | Live harness | Current evidence |
|---|---|---:|---:|---|
| PostgreSQL 17 catalog | Core candidate | Green | Available | Pending live |
| PostgreSQL 17 governed DataSource | Core candidate | Green | Available | Pending live |
| OpenAI cloud provider | Core candidate | Green | Available | Pending approved credentials/model |
| Ollama local/private provider | Core candidate | Green | Available | Pending approved runtime/model |
| MySQL 9.x driver path | Experimental | Green | Not yet automated | Pending live |
| MariaDB driver path | Experimental | Green | Not yet automated | Blocked by upstream packaged-driver advisory |
| Oracle 23ai-compatible path | Experimental | Green | Not yet automated | Pending live permissions/TLS |
| SQL Server 2022-compatible path | Experimental | Green | Not yet automated | Pending live permissions/TLS |
| Anthropic Claude | Experimental | Green | Available | Pending approved credentials/model |
| Google Gemini | Experimental | Green | Available | Pending approved credentials/model |
| Kimi/Moonshot | Experimental | Green | Available | Pending approved credentials/model |

The first generally usable self-hosted release should certify PostgreSQL plus at least one of the
two provider candidates. Other implemented adapters remain experimental until their rows are green;
they must not inherit support status from mock or protocol tests.

## Required evidence per certified combination

Record one row per database/driver/provider/model/prompt combination:

| Field | Requirement |
|---|---|
| Release and source revision | Immutable tag and commit SHA |
| Database and driver | Exact server and client versions |
| Provider and model | Provider id plus immutable model/version id |
| Prompt/evaluation | Prompt revision, dataset id/version/hash, runner version |
| Correctness | Offline gate, live execution accuracy, failure/truncation counts |
| Performance | Candidate latency p50 and p95; concurrency/load profile where applicable |
| FinOps | Mean and p95 cost per question in the configured pricing currency |
| Privacy/policy | Provider policy id, residency, retention, deployment type, maximum classification, provider-free preflight evidence, confirmation replay denial, and minimized transfer receipt |
| Operations | Migration, readiness, audit export, backup verification, restore drill result |
| Review | UTC timestamp, reviewer, workflow/report artifact link, expiry/retest date |

The manual `Live certification` workflow supplies the PostgreSQL and provider smoke evidence.
`sqlverity-live-certify` supplies answer-level result equivalence and candidate latency.
`sqlverity-load-test` supplies bounded HTTP throughput/latency/error evidence, while the scheduled
disaster-recovery workflow supplies isolated backup/restore evidence. Provider cost aggregation
remains a separate signed release artifact.
