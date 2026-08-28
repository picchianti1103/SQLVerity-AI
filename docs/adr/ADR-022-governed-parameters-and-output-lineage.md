# ADR-022: Governed generated-query parameters and output lineage

- **Status:** Accepted
- **Date:** 2026-08-24

## Context

Persisting user filter values inside generated SQL expands exposure and prevents a database plan from
being tied reliably to later execution. Whole-result masking is safe but unnecessarily hides public
outputs whenever a query also reads a sensitive column.

## Decision

Structured SQL proposals may declare up to 50 uniquely named scalar parameters. The supported
portable types are string, integer, number, boolean, ISO date, ISO datetime, and UUID. SQL must use
matching named `:parameter` placeholders; positional, undeclared, unused, LIMIT, and OFFSET bindings
fail validation. Values are checked at every lifecycle call and passed separately to database
drivers. PostgreSQL uses named pyformat, MySQL/MariaDB use connector pyformat, Oracle keeps named
bindings, and SQL Server converts validated named placeholders to ordered `?` bindings through its
AST. The ticket stores declarations, sorted names, and a deterministic SHA-256 value signature, but
never raw values. Approval and execution require the same signature previously recorded by EXPLAIN.

Validation also records each named output projection and its physical source columns. Direct
columns, aliases, scalar expressions, and CTE projections are resolved. The result processor applies
the highest source classification to each output and masks only outputs above the display policy.
Set operations, unnamed expressions, ambiguous derived sources, runtime column-name drift, or any
other unresolved shape mark lineage incomplete and retain the existing whole-result fallback.

## Consequences

- User-supplied filter values stay outside persisted SQL and audit payloads.
- Plan review, approval, and execution cannot silently substitute different bindings.
- Multi-dialect adapters receive their native parameter convention without string interpolation.
- Public outputs can remain visible beside masked sensitive outputs when lineage is proven complete.
- The signature is an integrity comparison, not a keyed MAC; HMAC or encrypted short-lived binding
  storage remains dependent on deployment key management.
- Arrays, composites, ranges, blobs, and parameters that alter query structure remain unsupported.
