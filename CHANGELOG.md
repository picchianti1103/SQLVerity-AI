# Changelog

All notable changes to SQLVerity AI are documented here. The project follows Semantic Versioning once a
public release is tagged.

## Unreleased

## 0.1.0 - 2026-08-28

### Added

- Initial public developer preview of the governed natural-language-to-SQL platform.
- English-first in-product guidance for the self-service console: a dependency-free locale catalog,
  state-aware Getting Started checklist, contextual explanations on every workspace, searchable Help
  drawer, glossary, keyboard dismissal, responsive layout, and regression coverage for public static
  assets and the no-browser-storage security boundary.
- Privacy-first AI egress onboarding: a dedicated pre-query privacy step, explicit deployment-bound
  policy acknowledgement, exact provider-free manifest preflight, safe server-classification reason
  codes, short-lived single-use confirmation bound to actor/policy/catalog/question/context, stable
  structured block errors, and append-only content-minimizing transfer receipts linked to token,
  latency, and cost telemetry. Confirmation nonces are consumed atomically in the shared catalog,
  and production PostgreSQL replicas require a common signing key.
- Governed named parameters for general generated SQL, including structured scalar declarations,
  exact AST placeholder matching, typed binding, value-signature integrity across EXPLAIN, approval,
  and execution, PostgreSQL/MySQL/MariaDB/Oracle/SQL Server driver adaptation, and Query Studio inputs
  that never persist raw values.
- Per-output-column AST lineage through direct projections, aliases, expressions, and CTEs, with
  selective classification masking and a conservative whole-result fallback when lineage cannot be
  proven complete.

- Operational pooled PostgreSQL catalog repository with packaged, advisory-locked migrations and
  readiness checks; Compose now starts a PostgreSQL 17 catalog and synthetic demo database.
- Vault KV v2 and AWS Secrets Manager database-secret resolution, fresh rotation-aware fetches, and
  audited connection testing that never persists credentials or a catalog snapshot.
- Deterministic server-side input classification and fail-closed provider egress policies scoped to
  tenants or DataSources, including purpose, maximum classification, residency, and retention.
- Catalog-coordinated per-user, tenant, and DataSource request/concurrency quotas, plus bounded REST
  provider retry and circuit breaking.
- Optional OIDC JWT/JWKS authentication with pre-provisioned subject binding and MFA/ACR checks.
- Browser OIDC Authorization Code + PKCE login, nonce/state validation, HttpOnly sessions, CSRF
  enforcement, federated-principal provisioning, and an administrative console for identity,
  provider policy, connection tests, and background jobs.
- A lease-based durable background worker with crash recovery, bounded retries, cancellation, and
  cursor-based semantic-inference batching.
- Request correlation, Prometheus metrics, structured content-free request logs, catalog readiness,
  authorized audit export, and checksummed SQLite/PostgreSQL backup and restore tooling.
- Opt-in W3C/OTLP OpenTelemetry tracing, Prometheus latency histograms and worker gauges, alert rules,
  operations runbook, append-only operational retention evidence, isolated restore drills, and a
  bounded load-test gate.
- Opt-in live PostgreSQL and real-provider tests, synthetic golden execution data, a result-equivalence
  certification runner, and a manual live-certification workflow.
- Structured intent interpretation for SQL proposals, including intent kind, natural-language
  summary, explicit row limit, governed table/column mappings, confidence, reasons, and alternatives.
- Query Studio interpretation panel and concrete ambiguity candidates in validation feedback.
- Deterministic Italian/English row-limit hints that prevent a generated proposal from silently
  changing an explicit request such as "prime dieci righe".
- DataSource-scoped intent-memory corrections from Query Studio. Authorized data stewards can
  confirm or replace a table/column mapping; repeated corrections supersede the active Business
  Concept while retaining immutable history, and changed mappings invalidate pending SQL tickets.
- Conversational intent correction in Query Studio: an LLM can interpret a free-text follow-up only
  against role-compatible objects from the current catalog. Low-confidence or ambiguous corrections
  request clarification without changing memory; accepted corrections use the same versioned,
  steward-authorized memory workflow.
- Privacy-selectable governed semantic retries for SQL generation. Maximum privacy keeps one
  deterministic attempt; explicit opt-in permits one semantic retry for recoverable intent-mapping
  failures and an on-demand semantic alternative after a successful proposal, without expanding the
  policy-filtered schema context or bypassing SQL validation.
- Public CI covering lint, strict typing, tests, the golden gate, package builds, and metadata checks.
- Git-history secret scanning plus dependency vulnerability and license inventory checks.
- Apache License 2.0 with SPDX package metadata.
- Optional database/provider installation extras.
- Non-root Docker and Compose quickstart with a persistent catalog volume.
- Multi-stage runtime image containing installed application packages rather than the source,
  documentation, tests, or local verification artifacts.
- Contributor, conduct, security, pull-request, and release-readiness documentation.
- Dependabot configuration for Python, GitHub Actions, and Docker base-image dependencies.

### Changed

- Gave the bounded CI load profile an explicit test-only request and concurrency budget so the
  container smoke gate measures runtime errors and latency instead of intentionally tripping the
  production-safe default user quota.
- Isolated the Compose demo workload from the application catalog in a separate database with a
  dedicated read-only role and repeatable initialization for existing volumes.
- Made quota concurrency crash-bounded to one request window and protected new-window counters from
  late releases belonging to an older window.
- Made background batch completion and continuation enqueue atomic, eliminating a lost-continuation
  window after successful work.
- Centralized catalog repository configuration, consolidated provider resource shutdown, and closed
  OIDC, LLM, Vault, AWS, tracing, worker, and repository resources during application shutdown.
- Tightened the load gate so authentication failures, redirects, and every non-2xx response other
  than separately reported throttling fail the error-rate threshold.
- Added a migration/rollback guide and refreshed operational, release-readiness, audit, and roadmap
  documentation against the implemented runtime.
- Reworked the README opening around the product workflow, developer-preview status, boundaries,
  and quickstart.
- Removed unused SQLAlchemy and Alembic runtime dependencies.
- Made the development extra self-contained for clean Linux type-checking by including the
  PostgreSQL and MySQL drivers imported by their adapters.
- Corrected the POSIX quoting in the hosted container-content smoke check.
- Pointed the hosted read-only container smoke test at its writable catalog volume.
- Updated the GitHub Actions checkout step to its Node.js 24-compatible major release.
- Updated the GitHub Actions Python setup step to its Node.js 24-compatible major release.
- Temporarily withheld the MariaDB packaging extra until the upstream Python driver publishes a
  release that resolves `PYSEC-2026-217`; the dialect and adapter remain implemented.
- Excluded internal planning material from the public distribution.
