# ADR-023: Privacy-first AI egress preflight and confirmation

- **Status:** Accepted
- **Date:** 2026-08-25

## Context

SQLVerity AI already evaluated classified prompt manifests against tenant and DataSource provider policies
before constructing a provider payload. That server-side boundary failed closed, but the normal user
journey exposed the policy only in Administration after Query Studio. Users could not inspect the
effective transfer before generation, server-side classification reasons were lost at the HTTP
boundary, and successful or blocked AI activity had no dedicated content-minimizing receipt.

Prompt disclosure must not require persisting or reconstructing the prompt, and a preview must never
call a provider. Confirmation must also be invalidated when any decision-relevant input changes.

## Decision

1. Privacy and AI sharing is a dedicated setup step before Query Studio. Administration retains a
   link for later maintenance but is not the first-use authorization surface.
2. SQL generation begins with a local preflight over the exact production retrieval context and
   structured request. The gateway exposes policy evaluation and manifest digesting independently
   from provider invocation.
3. Preflight returns provider/model, deployment type, purpose, effective policy and precedence,
   declared/detected/effective classification, safe detector codes, residency/retention claims,
   per-kind/classification counts, included/redacted identifiers, and maximum call count. It always
   reports `provider_invoked=false`.
4. Allowed preflights issue a short-lived HMAC confirmation containing no raw question or content.
   The token binds actor, tenant, DataSource, provider/model, purpose, catalog version, policy
   id/update timestamp, question digest, privacy mode, and full content-manifest digest. Tokens are
   single-use and protected against replay through an atomic catalog record shared by API replicas.
5. New allowing policies require an explicit acknowledgement. Its digest binds provider/model,
   deployment type, purpose set, classification ceiling, scope, residency, and retention. Existing
   or changed policies remain fail-closed at runtime and are exposed as `review_required` when their
   acknowledgement is missing or stale.
6. Prompt-egress failures use stable codes and safe metadata. They include whether the provider was
   invoked and may identify an opaque redacted item and kind, but never include prompt text, matched
   values, schema descriptions, credentials, parameters, rows, or provider responses.
7. Every preflight and terminal confirmation outcome records an append-only AI transfer receipt.
   Receipts contain classifications, safe reason codes, counts, policy and digest linkage,
   invocation status, and—after success—token, latency, and cost linkage. They do not retain content.

## Consequences

- The browser requires two explicit actions: local disclosure and confirm-and-send.
- Editing the question or changing context, policy, catalog, provider/model, actor, or privacy mode
  requires a new preflight.
- DataSource policy precedence is unchanged but becomes visible.
- `unspecified` residency and `provider_default` retention are rendered as absence of a recorded
  guarantee, not as EU residency or zero retention.
- Local/private providers receive the same disclosure and confirmation in the current implementation;
  a future organization policy may relax the confirmation gesture without removing disclosure.
- Confirmation nonces are issued and atomically consumed in the shared catalog, so replay is denied
  across API replicas and restarts. PostgreSQL deployments must provide the same secret
  `SQLVERITY_PREFLIGHT_SIGNING_KEY` to every replica; startup fails when it is absent.
- Privacy/legal stakeholder approval and live provider certification remain external release gates;
  this ADR records the technical boundary rather than contractual guarantees.
