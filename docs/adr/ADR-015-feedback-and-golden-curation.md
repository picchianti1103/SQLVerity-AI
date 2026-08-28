# ADR-015: Immutable query feedback and reviewed golden curation

- **Status:** Accepted
- **Date:** 2026-08-16

## Context

The corrected-SQL learning loop retained reviewed examples, but it did not record whether users
accepted, rejected, or corrected a generated query. Therefore acceptance and correction rates had
no governed denominator. Corrections could seed future prompts, but there was also no controlled
path for turning a proven correction into a regression case. Directly mutating the committed golden
dataset from runtime feedback would make releases non-reproducible and allow low-quality or
malicious feedback to weaken the gate.

## Decision

Each eligible QueryRequest may receive exactly one final `accepted`, `rejected`, or `corrected`
feedback event. Events are tenant/DataSource scoped, immutable in the database, and audited without
copying the reason, question, or SQL. Corrected feedback must reference the active corrected-SQL
revision whose `source_query_request_id` is the same request. Outcomes are allowed only after the
request has reached a reviewable state; failed and intermediate generation states cannot pollute the
denominator.

Acceptance rate is `accepted / total final feedback` and correction rate is `corrected / total final
feedback`. Both are `null` when no final feedback exists rather than fabricated as zero.

Golden curation is a separate two-step lifecycle:

1. A matching corrected feedback event may be promoted into an immutable candidate snapshot only
   while its corrected-SQL revision is active and all referenced tables and columns exist in the
   latest catalog version.
2. A human records one immutable final `approved` or `rejected` review. A candidate without a review
   remains `proposed`.

Only approved candidates appear in the deterministic format-versioned export. The export has no
wall-clock generation field and uses a stable candidate-id ordering. It carries the source request,
correction, catalog version, dialect, normalized SQL, lineage, concepts, assumptions, and content
classification needed for downstream curation. It does not alter the committed 50-case fixture.

SQLite enforces these invariants for local development and tests. PostgreSQL migration `0009`
provides the equivalent composite tenant foreign keys, uniqueness, checks, indexes, and immutable
update/delete triggers for production catalog deployment.

## Consequences

- Acceptance and correction rates now have real, auditable denominators.
- Feedback, candidature, and approval are distinct evidence records; no runtime event silently
  changes a release gate.
- Duplicate feedback, duplicate candidature, and second final reviews fail with conflicts.
- Schema-drifted or superseded corrections cannot be newly promoted.
- Audit records retain identifiers and counts but omit user-authored text and SQL.
- Approved exports remain deterministic, while merging them into a versioned fixture is an explicit
  release action that still needs duplicate, coverage, schema-fixture, and expected-result review.
- Actor ids still come from clients. Authentication, RBAC, and server-derived identities are the
  next required trust-boundary increment.
