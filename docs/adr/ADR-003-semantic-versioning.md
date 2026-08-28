# ADR-003: Semantic evidence, correction, and concurrency

- **Status:** Accepted for the MVP
- **Date:** 2026-08-09

## Context

Imported and inferred descriptions are evidence, not an unquestionable current truth. The platform
must let a steward confirm or correct them without erasing provenance, while preventing two review
sessions from silently overwriting each other. Object references are not globally unique: two data
sources in one tenant can both contain `public.orders` with different meanings.

## Decision

Store semantic definitions as append-only evidence. Database triggers reject updates and deletes.
Maintain the selected description separately in a semantic resolution keyed by
`(tenant_id, data_source_id, object_ref)`.

Passive evidence continues to use epistemic precedence. Evidence at equal authority with a different
meaning creates `CONFLICTING`; weaker evidence cannot replace stronger evidence. An explicit human
correction is different: it appends `CONFIRMED` evidence and deliberately selects it, including over
an earlier confirmed definition. The earlier evidence remains in history.

Corrections to an existing resolution require the client's `expected_updated_at` token. The write
returns `409 Conflict` when that token no longer matches. Resolution timestamps advance
monotonically even when the operating-system clock returns the same instant for rapid writes.

The correction records `actor_id` and an optional reason in immutable evidence and emits a
content-minimizing audit event. During this unauthenticated development slice, `actor_id` is supplied
by the API client. Production authentication must derive it from the verified identity and must not
trust this request field.

## Consequences

- Reviewers can audit every proposed, confirmed, and superseded meaning.
- Inferred or conflicting resolutions can be listed without mixing data sources.
- Concurrent review sessions fail explicitly instead of silently losing work.
- A later authentication slice must replace client-supplied actor identity.
- PostgreSQL deployments must apply the updated catalog migration before using these endpoints.
