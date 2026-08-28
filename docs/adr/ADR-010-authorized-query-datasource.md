# ADR-010: Authorized query as a virtual DataSource

- **Status:** Accepted for the MVP baseline
- **Date:** 2026-08-09

## Context

Some organizations cannot grant catalog-wide or table-level access. They can expose only a reviewed
base query over a restricted view. Treating that query as free-form SQL would allow unsafe string
composition, hide its schema from retrieval, and make parameter values or policy changes difficult
to audit. The platform needs to generate useful outer queries without expanding access beyond the
authorized surface.

## Decision

Represent each authorized query as an immutable definition tied to a new DataSource catalog version.
The catalog version contains exactly one `VIRTUAL_QUERY` object with an explicit ordered output
schema and imported semantics. The definition stores the reviewed base SQL, normalized PostgreSQL
SQL, named parameter declarations, and filtering/aggregation capabilities. A later definition
creates a later catalog version; pending requests against the previous version become stale through
the existing catalog-drift rule.

Accept only one PostgreSQL `SELECT` as base SQL. Reject writes, administrative nodes, row locks,
`SELECT INTO`, wildcard outputs, cross-catalog references, unsafe or unknown anonymous functions,
undeclared/unused placeholders, duplicate output names, and projections that do not match the
declared schema in order.

Keep LLM-generated SQL scoped to the virtual object and validate it with the existing SQL safety
boundary. Immediately before EXPLAIN or execution, parse that SQL again and replace its single
virtual-table AST node with the normalized base-query AST. Never concatenate SQL fragments. SQLGlot
renders declared placeholders in the mapping form accepted by the PostgreSQL driver.

Parameter values are supplied separately as JSON scalars and are never interpolated into SQL,
persisted in query tickets, or copied into audit events. Persist only sorted parameter names and a
canonical SHA-256 signature containing the definition id and values. Authorized queries always
require EXPLAIN before approval. Approval, later EXPLAIN calls, and execution must reproduce the same
signature, preventing parameter substitution after planner review.

Require `EXPLAIN`, `EXECUTE_READ_ONLY`, a connection secret reference, application SQL policy, and
the existing database read-only transaction boundary. An Authorized Query DataSource is forbidden
from declaring `INTROSPECT`, preventing the generic catalog-ingestion route from expanding the
visible surface. The database account remains the final least-privilege control.

## Consequences

- Retrieval and SQL generation see a stable table-like schema without seeing or targeting arbitrary
  underlying tables.
- Base-query revisions and virtual-schema revisions are versioned together and auditable.
- Output projection names are verified statically; physical output types require a live database
  check and remain unverified in this environment.
- Binding currently accepts scalar JSON values. Arrays, composite types, and organization-specific
  safe UDFs remain fail-closed.
- One definition exposes one virtual object and the outer query can reference it once. Multi-surface
  governed joins require an explicit future policy.
- A plain signature avoids retaining raw values but is not a secret-key integrity primitive. HMAC or
  encrypted short-lived binding storage depends on deployment key management.
