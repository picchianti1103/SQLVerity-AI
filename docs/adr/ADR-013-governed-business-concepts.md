# ADR-013: Governed Business Concepts and Synonyms

- **Status:** Accepted for the Phase 8 baseline
- **Date:** 2026-08-16

## Context

Business users ask for concepts such as “fatturato lordo” or “cliente attivo”, while physical
catalogs expose names such as `orders.total_amount`. Corrected SQL examples can bridge language for
one similar question, but they do not provide a reusable definition, an explicit synonym set, or a
governed mapping to physical objects. The structural document requires business concepts and
synonyms to retain epistemic state and correction history. Inference must never silently replace
confirmed knowledge.

A global synonym registry would also be unsafe: the same phrase can legitimately mean different
things in two DataSources. Merging competing evidence field by field would produce a definition
that no source actually asserted.

## Decision

Represent every proposal or correction as an immutable `BusinessConceptDefinition`, scoped by
tenant and DataSource and bound to the latest catalog version. A definition contains a stable
lowercase key, display name, description, synonyms, physical table/column references, content
classification, epistemic status, confidence, source, optional actor and reason. Database triggers
reject updates and deletes.

Maintain a separate mutable `BusinessConceptResolution` for the current meaning. Apply whole-record
precedence (`CONFIRMED > IMPORTED > INFERRED > UNKNOWN`): higher authority may supersede lower
authority, lower authority remains historical evidence, and different evidence at equal authority
marks the resolution `CONFLICTING`. No partial merge is performed. Only a human correction can
create `CONFIRMED` evidence through the public service. Correcting an existing resolution requires
its exact `updated_at`, so stale writers fail with a conflict.

Normalize terms with Unicode accent folding, case folding, punctuation removal, and whitespace
normalization. Resolve only `CONFIRMED` concepts for query generation. A name, key, or synonym may
match a phrase in the question. Confirming a term already owned by another confirmed concept in the
same DataSource fails with an explicit conflict. The resolution result also represents dynamic
ambiguities defensively, so directly imported legacy collisions cannot be selected silently.

Use each matched concept’s physical references as deterministic Context Builder seeds. Referenced
columns receive a selection boost. Expose a concept only when all of its references survived the
final context limits. Its prompt classification is the maximum of the declared concept
classification and referenced-column classifications. The LLM gateway may redact it, and generated
`business_concepts` must be a subset of the non-redacted governed concept keys.

Audit identifiers, states, actions, actor, and counts only. Do not duplicate descriptions or
synonyms into audit events.

## Consequences

- Business vocabulary can select the correct physical schema without relying on table-name overlap
  or a previously corrected question.
- Proposals, conflicts, reviews, corrections, and evidence history are inspectable and tenant/
  DataSource isolated.
- Confirmed knowledge cannot be overwritten by inference, and stale corrections fail closed.
- Synonym collisions and provider-egress redaction cannot silently change query provenance.
- This increment does not define metric formulas, grain, dimensions, filters, business-rule
  dependencies, authentication/RBAC, background concept inference, or a UI editor. Those remain
  separate roadmap increments or environmental gaps.
