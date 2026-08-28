# ADR-011: Versioned golden dataset and fail-closed regression gate

- **Status:** Accepted for the MVP baseline
- **Date:** 2026-08-09

## Context

The platform already has deterministic boundaries for retrieval, SQL validation, execution,
privacy, and cost governance, but unit tests alone do not reveal whether a model or prompt change
alters the behavior of representative user questions. The structural document requires at least 50
golden questions, multiple realistic demonstration schemas, semantic and safety measurements, and
model-to-model regression checks.

Execution accuracy, database latency, provider cost, context-token efficiency, and user correction
rate need live systems or observed feedback. Reporting invented values for them would make a release
gate look more complete than the available evidence permits.

## Decision

Keep a strict, versioned JSON dataset with 50 cases over commerce, finance, and support contexts.
Each case declares its question, expected disposition, allowed physical context, expected concepts,
safety issue codes, and a reference structured proposal. Accepted cases have canonical PostgreSQL;
clarification cases have no SQL and state an ambiguity; rejected cases exercise a named safety
boundary.

Run every proposal through the production PostgreSQL AST validator. For accepted cases, require the
expected disposition, exact physical lineage, required business concepts, and SQL equivalence after
normalization and removal of the validator-managed preview limit. For clarification cases, require
empty SQL and at least one ambiguity. For rejected cases, require all expected safety issue codes.

Version three release artifacts independently:

- the dataset, whose canonical JSON content is bound by SHA-256;
- thresholds, currently requiring 50 cases, perfect deterministic rates, and zero regressions;
- the baseline, tied to dataset id, dataset version, dataset hash, runner version, and every case id.

External model or prompt runs supply one full structured proposal per case through a predictions
file bound to the dataset id, version, and hash. Missing, duplicate, or unexpected ids fail closed.
The command exits non-zero when a threshold is missed, a previously passing case fails, or the
baseline does not match the dataset or runner. Reference proposals are used only when no predictions
file is supplied, providing a deterministic integrity check for the evaluation machinery and
committed artifacts.

Report offline metrics for case pass rate, SQL/semantic accuracy, semantic correctness, safety,
clarification precision and recall, and first-pass validation acceptance. Represent execution
accuracy as `null` and enumerate every unavailable KPI with a reason instead of substituting a
proxy.

## Consequences

- A model, prompt, validator, or policy change can be compared against a reproducible release
  baseline and attributed to exact case ids.
- Safety and clarification behavior are first-class release criteria, not incidental examples.
- Editing the dataset invalidates the committed baseline until the change is reviewed and a new
  baseline is intentionally accepted.
- The reference-only run proves deterministic validation, not LLM quality or database result
  correctness. Those measurements remain blocked until live provider and PostgreSQL fixtures exist.
- Exact normalized SQL is deliberately stricter than broad relational equivalence. Reviewed
  alternatives can be added explicitly per case; a future live executor can add answer-level
  equivalence without weakening the current gate.
