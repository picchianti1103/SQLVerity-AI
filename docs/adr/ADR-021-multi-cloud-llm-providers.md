# ADR-021: Multi-cloud LLM providers

## Status

Accepted on 2026-08-16.

## Context

SQLVerity AI already had a provider-neutral gateway, an OpenAI cloud adapter, and an Ollama local/private
adapter. A self-contained deployment needs to support additional approved model vendors without
bypassing prompt-egress policy, structured output, FinOps preflight, usage accounting, or the
governed query lifecycle. The requested initial set is Anthropic Claude, Google Gemini, and
Kimi/Moonshot, and one instance may need to expose more than one provider.

Provider APIs differ in request shape, completion status, usage telemetry, and data-retention
controls. A generic configurable base URL would also turn model configuration into an unrestricted
server-side egress channel.

## Decision

- Add first-class `anthropic`, `gemini`, and `kimi` adapters behind the existing `LLMProvider`
  contract. `claude` aliases `anthropic`; `moonshot` aliases `kimi`.
- Use direct official HTTPS REST contracts through the existing `httpx` dependency. Endpoints are
  fixed in code: Anthropic Messages, Gemini `generateContent`, and Kimi Chat Completions.
- Require schema-constrained JSON output: Anthropic `output_config.format`, Gemini
  `responseJsonSchema` with `application/json`, and Kimi `response_format=json_schema`.
- Keep credentials server-side and out of representations, logs, catalog records, browser storage,
  and request payloads. Every cloud provider requires an explicit API key and exact model id.
- Keep activation explicit. `SQLVERITY_LLM_PROVIDERS` accepts a comma-separated set; the legacy
  `SQLVERITY_LLM_PROVIDER` accepts one provider. Setting both, selecting duplicates, or selecting an
  unknown or incompletely configured provider prevents startup. Credentials alone activate
  nothing.
- Separate trusted system instructions from serialized untrusted input, set deterministic
  generation controls, bound output tokens and timeouts, and conservatively overestimate tokens
  before calls. Use measured provider telemetry for actual usage and cached input when present.
- Accept only a single normally completed structured response. Refusal, safety/length termination,
  malformed JSON, missing telemetry, ambiguous alternatives, and transport failures fail closed and
  are normalized without exposing upstream response bodies or secrets.
- Do not contact a model provider during startup or health checks. Provider availability remains a
  runtime call concern.
- Report cloud-provider response storage as `provider_policy`. Unlike the OpenAI adapter's explicit
  `store=false`, these adapters do not claim a retention guarantee that is not present in their
  request contracts. Production deployment must assess the selected account's residency,
  retention, and zero-data-retention policy.

## Consequences

- The API can host OpenAI, Anthropic, Gemini, Kimi, and Ollama simultaneously, and the console can
  discover their stable provider ids without provider-specific frontend code.
- Adding a provider does not add a new SDK dependency or create a new policy/FinOps path, but each
  protocol parser remains intentionally explicit and independently tested.
- Offline tests verify request and response contracts with injected clients. Live interoperability,
  provider policy, model availability, quota behavior, and billable telemetry remain external gates
  until approved non-production credentials and exact model ids are provided.
- Tenant budgets still require effective pricing for every provider/model combination that may be
  selected.

## References

- Anthropic structured outputs: <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>
- Gemini structured output: <https://ai.google.dev/gemini-api/docs/structured-output>
- Gemini REST `generateContent`: <https://ai.google.dev/api/generate-content>
- Kimi Chat API: <https://platform.kimi.ai/docs/api/chat>
- Kimi API overview: <https://platform.kimi.ai/docs/api/overview>
