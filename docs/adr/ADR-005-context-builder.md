# ADR-005: Deterministic Context Builder baseline

- **Status:** Accepted for the MVP baseline
- **Date:** 2026-08-09

## Context

Sending a complete enterprise schema to an LLM is inefficient and increases privacy, cost, and
prompt-injection exposure. The platform must show exactly which subset it selected and must not
pretend that embeddings or governed business concepts exist before their storage and evaluation
models are implemented.

## Decision

Start with deterministic lexical retrieval over the latest tenant-scoped catalog version. Rank
schema objects using exact physical names, column names, and only `CONFIRMED` descriptions. Imported
or inferred descriptions remain visible elsewhere but do not influence this governed retrieval
baseline.

Select a bounded number of lexical seed objects, then perform bounded breadth-first expansion across
non-conflicting relationships. Include relationships only when both endpoints are selected. For each
object, retain primary keys, relationship keys, and query-matching columns first; fill remaining
capacity by physical ordinal. Essential columns may exceed the target limit so composite keys and
query evidence are not silently broken.

If no object matches, fail closed rather than falling back to the full schema. Return scores,
selection reasons, graph-expansion markers, omitted counts, classifications, and catalog version in
an inspectable context preview.

SQL generation consumes this context through the LLM Gateway. The user question and target dialect
are classified, required manifest items. Policy redaction of either blocks the call. Provider output
must match the `SQLProposal` structure and reference only non-redacted context objects. The SQL text
itself is intentionally marked `not_validated` and cannot be executed in this slice.

## Consequences

- Schema selection is reproducible, explainable, and testable without an embedding service.
- Relationship expansion supplies join context without sending unrelated catalog objects.
- Sensitive columns can be removed before the provider call and cannot reappear in declared output.
- Lexical retrieval does not resolve synonyms or semantic similarity; embeddings and reranking remain
  deferred and are tracked explicitly.
- AST validation remains mandatory before preview or execution can be enabled.

The intentionally unvalidated output boundary described here was completed by ADR-006. This ADR
continues to define context selection and prompt egress behavior.
