# ADR-007: Persisted approval and PostgreSQL read-only execution

- **Status:** Accepted for the MVP baseline
- **Date:** 2026-08-09

## Context

An accepted AST is not sufficient authorization to run arbitrary client-supplied text. The platform
must preserve the exact validated SQL, bind it to its tenant, DataSource, and catalog version, expose
cost evidence before execution, require explicit approval, and retain only safe operational metadata
in the audit log. Database permissions remain an independent safety boundary.

## Decision

Persist a query ticket after structured generation and validation. The ticket stores the provider SQL,
normalized SQL, physical lineage, validation issue codes, catalog version, and lifecycle state. API
execution endpoints accept only the ticket identifier; they never accept replacement SQL. A newer
catalog version makes the ticket stale. SQL is parsed and validated again immediately before approval,
`EXPLAIN`, and execution, followed by the SQL-access policy check.

Allow JSON `EXPLAIN` without `ANALYZE` while a ticket is `READY_FOR_PREVIEW` or `APPROVED`. Require an
explicit actor-labelled transition to `APPROVED` before read-only execution. The PostgreSQL adapter
requires declared DataSource capabilities and an opaque connection-secret reference, starts a
read-only transaction, configures a bounded server-side `statement_timeout`, and fetches result rows
in small batches. Serialized output is capped by rows and bytes. An active connection registry enables
PostgreSQL cancellation when the DataSource declares that capability.

Successful execution moves through `EXECUTING`, `SUCCEEDED`, `RESULT_PROCESSING`, and `COMPLETED`.
Adapter failures move an active request to `FAILED_EXECUTION`; cancellation moves eligible states to
`CANCELLED`. Audit events store state changes, cost estimates, counts, timings, truncation, and byte
size, but never plan bodies or returned rows.

This original parameter-free boundary was superseded on 2026-08-24 by ADR-022. General proposals may
now declare governed named scalar parameters; their values are type-checked, bound separately by the
driver, and signature-bound across `EXPLAIN`, approval, and execution without being persisted.

## Consequences

- A client cannot change SQL between validation, approval, and execution.
- Catalog drift, missing capabilities, missing secrets, policy denial, and invalid state all fail before
  database execution.
- `EXPLAIN` supplies planner cost evidence but no automatic cost threshold is enforced yet.
- The application cannot prove that the configured database account has least-privilege permissions;
  operators must provision that account independently.
- Application byte limits prevent oversized responses but cannot stop the database driver from first
  allocating one oversized field. Database-side value policies remain future work.
- Actor identity is still client-supplied because authentication and RBAC are not implemented.

Result classification, redaction, deterministic summaries, and provenance were implemented by
ADR-008. This ADR remains authoritative for approval and database execution controls.
