# ADR-012: Corrected SQL as immutable retrieval evidence

- **Status:** Accepted for the Phase 8 baseline
- **Date:** 2026-08-16

## Context

The structural document defines the learning loop as governed customer-specific knowledge, not
training of the foundation model. An authorized user corrects a query, the correction is saved as
new evidence, linked to the question, DataSource, and schema version, and later retrieved for similar
questions. Previous revisions must remain inspectable, incompatible knowledge must not silently
enter a new context, and sensitive content must still obey provider-egress policy.

Persisting only the latest corrected SQL would erase provenance. Passing every historical example
to a model would create schema-drift, privacy, prompt-injection, and context-budget risks.

## Decision

Store each human correction as an immutable `CorrectedSQLExample`. It contains the original
question, a deterministic normalized question, original and normalized SQL, AST-derived physical
lineage, business concepts, assumptions, content classification, actor, reason, optional source
QueryRequest, the exact catalog version, and an optional predecessor.

Use append-only revision chains. Revision one has no predecessor; every later revision explicitly
names the currently active predecessor. Partial unique indexes allow one root per normalized
question and one successor per revision. Database triggers reject updates and deletes. Active state
is derived from the absence of a successor, so superseding evidence never mutates old evidence.
Concurrent or stale writers fail with a conflict.

Validate corrected SQL through the production PostgreSQL AST safety boundary before persistence.
The service first discovers physical lineage, then validates again with exactly those declared
references against the latest catalog. Writes, administrative operations, locks, wildcard
projections, unsafe functions, unknown objects or columns, and all other existing safety failures
remain prohibited. The stored normalized SQL therefore has the same bounded preview contract as a
generated proposal.

Retrieve only active examples in the same tenant and DataSource. Rank them deterministically using
normalized-question equality followed by token Jaccard similarity. Before returning a match,
require every stored table and column to still exist in the latest catalog. Schema-incompatible
examples remain visible as historical evidence but cannot influence generation.

Let corrected examples participate in Context Builder seed selection. Referenced columns receive a
deterministic selection boost, and an example is exposed in the final context only when all of its
lineage is present. For prompt egress, classify the whole example at the maximum of its declared
content classification and the current classifications of its referenced columns. Sensitive
examples are redacted by the LLM Gateway. Included examples are still labelled untrusted prompt
content and are patterns, never executable instructions.

Audit only ids, revision, catalog/DataSource links, lineage counts, and predecessor/source links.
Do not copy the question or SQL into audit events.

## Consequences

- Confirmed business language can retrieve the correct physical objects even when the phrase does
  not occur in table or column names.
- Every correction has an inspectable author, reason, schema version, and immutable history.
- Schema drift and stale concurrent edits fail closed without deleting accumulated knowledge.
- Provider prompts can benefit from eligible examples while preserving classification-based egress
  controls.
- This increment does not define governed metrics, business rules, or synonyms and does not
  automatically promote corrections into the golden dataset. Those remain separate Phase 8 work.
- A correction count is now available, but correction rate still needs a governed denominator and
  user-feedback lifecycle; it is not inferred from incomplete events.
