# ADR-008: Deterministic local result processing

- **Status:** Accepted for the MVP baseline
- **Date:** 2026-08-09

## Context

Database results can contain more sensitive information than the schema metadata used to generate a
query. Automatically sending rows to an LLM would violate the metadata-only baseline and would make
simple formatting dependent on a probabilistic external service. The product must return a readable
answer while preserving evidence, costs, truncation, classification, and an explicit account of data
egress.

## Decision

Process every successful read-only result locally and deterministically. Classify the result from the
physical columns recorded in the validated query ticket and reload those classifications from the
same catalog version immediately before execution. A missing classification is a validation failure;
inside the processor it also defaults to `HIGHLY_SENSITIVE` as defense in depth.

Produce four distinct response sections:

- the bounded table returned by the executor;
- a deterministic summary selected from empty, single-value, single-row, or table shape;
- a privacy report with maximum classification, classification counts, masking, lineage confidence,
  processing mode, and explicit raw-row/LLM flags;
- provenance containing SQL, DataSource, catalog version, physical lineage, concepts, assumptions,
  approval, planner estimates, execution metadata, model/provider, and linked LLM costs.

The default display policy permits `PUBLIC` and `INTERNAL`. If any referenced column is more
sensitive, redact every non-null result value before returning it through the API. This whole-result
strategy is conservative because aliases and computed expressions do not yet have output-level
lineage. Never persist rows, deterministic answer text, or full query plans in audit events. Audit
only classification and operational metadata. Scalar summaries are length-bounded.

## Consequences

- Raw rows are never sent to an LLM or placed in an LLM-ready payload in this increment.
- Simple answers do not add model latency, token usage, or nondeterminism.
- Sensitive results can be executed locally after approval but are redacted at the API display
  boundary under the default policy.
- Aggregation-aware semantic wording remains unavailable until governed metrics exist.
- Alias/expression lineage can over-redact safe outputs; precise AST projection lineage is deferred.
- Optional narrative interpretation requires a separate, explicit result-egress policy and is not a
  fallback path.
