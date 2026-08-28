# ADR-006: PostgreSQL AST safety boundary

- **Status:** Accepted for the MVP baseline
- **Date:** 2026-08-09

## Context

Structured LLM output is untrusted. Checking only the provider-declared tables or searching SQL text
for forbidden words cannot establish that a statement is read-only, uses the governed schema
context, or has a safe preview bound. PostgreSQL is the first MVP dialect, so validation must use
PostgreSQL parsing semantics rather than a generic SQL approximation.

## Decision

Parse provider SQL with SQLGlot's PostgreSQL dialect and require exactly one read-only query rooted
in `SELECT`, `UNION`, `INTERSECT`, or `EXCEPT`. Read-only CTEs are supported. Reject all detected
write and administrative AST nodes, `SELECT INTO`, row-locking clauses, wildcard projections except
`COUNT(*)`, known dangerous functions, and anonymous functions not present in the conservative
allowlist.

Resolve physical tables and columns through query scopes, aliases, CTEs, subqueries, and set
operations. Every physical reference must occur in the non-redacted context passed to the provider,
and the reference sets declared in the structured proposal must exactly match the AST-derived sets.
Ambiguous unqualified references fail closed. Cross-catalog references are prohibited.

For accepted queries, normalize SQL in the PostgreSQL dialect and add a preview `LIMIT` when absent.
Cap a larger static limit at the configured maximum and reject dynamic limits. Validation returns
machine-readable blocking and informational issues. Rejected queries never receive normalized
preview SQL. Passing validation means only `ready_for_preview`; `ready_for_execution` remains false.

## Consequences

- Prompt instructions and structured output validation are no longer trusted as the SQL safety
  mechanism.
- CTE and alias resolution preserves the lineage of governed physical objects while derived objects
  are not mistaken for catalog tables.
- Unknown functions and ambiguous references can reject safe SQL; this is an intentional fail-closed
  tradeoff until governed per-source policies exist.
- The validator does not replace database read-only credentials, transaction read-only mode,
  statement timeouts, cancellation, row caps, or approval checks. Those controls remain mandatory at
  the future execution boundary.
- PostgreSQL is the only accepted dialect in this increment. Each additional dialect requires its
  own parsed capability profile and regression corpus.

The database controls described above were implemented by ADR-007. This ADR remains authoritative
for AST validation and normalization.
