# ADR-017: Concrete OpenAI Responses API provider

- **Status:** Accepted
- **Date:** 2026-08-16

## Context

ADR-004 established a provider-neutral LLM Gateway but deliberately left external SDK behavior and
credentials outside the core. The structural document requires at least one concrete cloud provider
for the MVP. The application therefore returned `503` for every generation request unless tests or
embedding code replaced the gateway manually.

The provider must preserve the existing prompt-egress boundary, produce JSON that downstream domain
services can validate again, supply truthful usage telemetry for FinOps, avoid persisting prompt
content, and never enable external egress merely because credentials happen to exist in the process
environment.

## Decision

Add an `OpenAIResponsesProvider` implemented against the official Python SDK and the Responses API.
It uses top-level trusted `instructions`, serializes the already policy-filtered gateway input as one
untrusted JSON value, and requests strict Structured Outputs with `text.format`, `type=json_schema`,
and the gateway's output schema. Responses are stateless (`store=false`) and no tools are enabled.

The adapter accepts a response only when it is complete, contains a non-empty model id, contains
valid JSON whose root is an object, and provides non-negative input/output usage. Cached input tokens
are captured separately and cannot exceed total input tokens. SDK transport failures and malformed
provider responses become typed adapter errors; the gateway continues to expose only its generic
provider-call error at the application boundary. Prompt and response bodies are not logged or added
to usage records.

Pre-call token counting uses a deliberately conservative local estimate: one token per serialized
UTF-8 byte, fixed protocol overhead, and the configured maximum output-token budget. This can
overestimate cost, but it preserves fail-closed tenant budget authorization without making a second
remote request. Actual usage from the response drives final cost accounting.

Configuration is explicit and environment-only:

- `SQLVERITY_LLM_PROVIDER=openai` enables the provider;
- `OPENAI_API_KEY` and `SQLVERITY_OPENAI_MODEL` are required;
- timeout, retry count, and maximum output tokens have bounded optional settings;
- the API base URL is fixed to `https://api.openai.com/v1` so an ambient SDK environment variable
  cannot silently redirect governed metadata to another host.

The API remains provider-free when the selector is absent, even if an OpenAI key exists. Unknown or
incomplete provider configuration fails during startup before the catalog resource is opened. The
configuration health result is local-only and never performs an unexpected network request.

## Consequences

- SQL generation and semantic inference can use provider id `openai` without replacing application
  state manually.
- A leaked or inherited `OPENAI_API_KEY` cannot enable prompt egress by itself.
- The selected model remains deployment configuration and must have tenant pricing when an active
  FinOps budget exists.
- Unit and API-startup tests use simulated clients; no secret or billable request is needed for the
  offline gate.
- A live non-production credential/model is still required for external end-to-end and cross-model
  golden evaluation.
- The remaining explicit MVP provider gap is a concrete local/private LLM adapter.
