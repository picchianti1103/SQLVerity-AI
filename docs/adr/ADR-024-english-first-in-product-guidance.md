# ADR-024: English-first console and versioned in-product guidance

- **Status:** Accepted
- **Date:** 2026-08-25

## Context

The self-service console exposed the governed SQL lifecycle, but its copy was primarily Italian and
users had to infer setup order and security boundaries from the interface. That made the product less
portable across organizations and increased the chance that a first-time user would discover a
prerequisite only after an operation failed.

Guidance must describe the actual product state, stay aligned with each release, work under the
same-origin content-security policy, and preserve the existing browser-memory-only treatment of API
keys, query parameters, questions, and results.

## Decision

1. English is the canonical and default console locale. The document language and all static and
   runtime user-facing copy use English; locale is not inferred from the browser.
2. A dependency-free message catalog owns shared runtime strings and provides interpolation plus
   declarative translation hooks. Its public API leaves room for reviewed future locales without
   coupling the application to a frontend framework.
3. A separate versioned guidance registry owns page explanations, task-oriented help topics, a
   glossary, recommended next steps, and the Getting Started sequence. Guidance ships with the same
   source revision as the behavior it explains.
4. Getting Started status is derived from live in-memory application state: authentication, tenant,
   DataSource, loaded schema, effective privacy policy, reviewed preflight, and generated proposal.
   Editing any preflight-bound input invalidates that progress just as it invalidates the preflight.
5. Every primary workspace exposes contextual help and the global searchable drawer remains
   keyboard-dismissable, responsive, and rendered only through text-safe DOM operations.
6. No locale preference or guidance state is written to local storage, session storage, cookies, or
   the catalog. Authentication material and governed query data retain their existing boundaries.

## Consequences

- New canonical UI copy and guidance are authored in English and covered by the console regression
  test. A future locale must provide a reviewed catalog and explicitly selected preference rather
  than silently following browser locale.
- Guidance can evolve independently from API logic, but changes that describe security, privacy, or
  execution behavior must be reviewed with the implementation in the same change.
- Onboarding is intentionally a transparent progress aid, not an authorization mechanism. Server
  RBAC, egress policy, preflight confirmation, validation, approval, and execution controls remain
  authoritative.
- The first implementation contains curated task guidance rather than telemetry-driven tours or an
  AI help assistant. Those features would require separate consent, privacy, and support-lifecycle
  decisions.
