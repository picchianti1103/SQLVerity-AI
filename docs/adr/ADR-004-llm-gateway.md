# ADR-004: Provider-neutral LLM Gateway and prompt egress

- **Status:** Accepted for the MVP
- **Date:** 2026-08-09

## Context

Semantic inference needs an LLM without coupling catalog rules to a provider SDK. Schema names and
descriptions can still be sensitive or hostile, so building a prompt before evaluating policy would
violate the platform's privacy and prompt-injection boundaries. Provider output cannot be trusted
merely because structured output was requested.

## Decision

Place every provider behind the domain `LLMProvider` interface. The gateway accepts a typed request
containing trusted instructions, individually classified content items, and an output schema. It
first sends a content-only manifest to the policy engine. Only after an allow decision does it omit
redacted item identifiers and construct the provider payload. Retrieved schema is explicitly marked
as untrusted data and raw rows are rejected by the initial metadata-only policy.

A provider must advertise guaranteed structured output. The semantic inference service then applies
its own strict validation: exact fields, known non-redacted object references, no duplicates,
bounded descriptions and reasons, and confidence between zero and one. Any invalid proposal rejects
the entire output before semantic evidence is written.

Valid proposals are appended as `INFERRED` evidence. Existing imported, confirmed, or conflicting
resolutions are not inference targets, and domain precedence remains the final safeguard against a
concurrent lower-authority overwrite.

Every successful provider call creates an append-only usage event containing provider, model,
purpose, estimated and actual token counts, latency, and optional cost fields. Prompt and response
content are not stored in usage or audit events. Prices remain external and versionable rather than
hardcoded.

The default API runtime registers no external provider and therefore fails closed with `503` until
an adapter is explicitly configured. Provider-specific authentication and SDK behavior stay outside
the gateway core.

## Consequences

- Catalog and inference behavior can be tested with injected providers.
- Policy-redacted objects cannot be reintroduced through a malicious provider response.
- Calls remain observable without turning telemetry into a copy of prompt content.
- A concrete cloud or local provider adapter and its credential configuration remain required.
- Context retrieval and batching will be needed before sending large catalogs to a provider.
