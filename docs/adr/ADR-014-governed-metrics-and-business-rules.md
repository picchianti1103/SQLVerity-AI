# ADR-014: Governed Metric Definitions and Business Rules

- **Status:** Accepted for the Phase 8 baseline
- **Date:** 2026-08-16

## Context

A confirmed business concept explains vocabulary and physical meaning, but it does not define how a
metric is calculated or which rows are valid. Letting a model invent aggregation formulas, grain,
dimensions, and filters on every request would make identical questions produce incompatible
answers while still appearing to share the same concept provenance.

Metric and rule text also contains executable-looking SQL. Treating it as an unchecked string would
allow multiple statements, subqueries, unsafe functions, unqualified lineage, or hidden filters to
enter the semantic catalog and later the provider prompt.

## Decision

Model `MetricDefinition` and `BusinessRuleDefinition` as immutable, catalog-version-bound evidence
with tenant and DataSource scope. Keep current `MetricResolution` and `BusinessRuleResolution`
records separately. Apply the same whole-record epistemic precedence used elsewhere:
`CONFIRMED > IMPORTED > INFERRED > UNKNOWN`; equal-authority disagreement becomes `CONFLICTING`,
lower evidence cannot replace confirmed truth, and only a human correction can create confirmed
evidence. Existing resolutions require an exact `updated_at` token for correction.

A metric records a stable key, name, description, aggregate expression, explicit physical grain,
allowed dimensions, referenced columns, confirmed concept dependencies, confirmed Business Rule
dependencies, classification, provenance, and confidence. A Business Rule records a key, name,
description, row predicate, referenced columns, confirmed concept dependencies, classification,
provenance, and confidence. Confirmed names must be unique within their asset kind and DataSource.

Validate every formula and predicate with SQLGlot using the DataSource dialect. The current baseline
supports PostgreSQL fragments only. Columns must use `schema.table.column`; references must exist in
the latest catalog. Metric formulas require an aggregate and reject statements, subqueries,
wildcards, aliases, windows, embedded predicates, `FILTER`, and `CASE`; row filtering belongs in a
Business Rule. Rules require a boolean row predicate and reject aggregates, subqueries, windows,
parameters, and non-allowlisted anonymous functions. Every fragment is stored with deterministic
normalization.

Store both asset kinds in append-only analytic-semantic evidence and current-resolution tables using
a kind discriminator and typed JSON payload. Database triggers reject evidence updates/deletes.
Audit only ids, kind, state transitions, actors, and dependency/reference counts.

Resolve only confirmed assets. A metric or rule can match its governed name/key or a confirmed
Business Concept dependency. Metric rule dependencies pull their rules into context. Their physical
columns seed Context Builder selection. Prompt classification is the maximum of the asset,
referenced columns, and—on metrics—all dependent rules and their columns. A metric cannot survive
egress redaction when one of its required rules is unavailable.

Generated proposals may declare only non-redacted metric/rule keys. Declaring a metric requires all
of its dependent rules in provenance. A second AST check requires the exact normalized metric
expression inside a SELECT projection and each declared rule predicate in a filtering position
(`WHERE`, `HAVING`, `JOIN ON`, or aggregate `FILTER`). A declaration is rejected if the SQL does not
actually apply the governed definition.

## Consequences

- Formula, grain, dimensions, filters, dependencies, evidence, and approval state are inspectable.
- A model cannot certify altered SQL by merely copying a governed metric or rule key into metadata.
- Sensitive rule dependencies propagate to metrics and cannot be partially redacted.
- Query tickets and result provenance now retain metric and Business Rule keys in addition to
  concepts and assumptions.
- This baseline does not support derived metric-to-metric DAGs, semantic calendars, units/currency
  conversion, formatting contracts, non-row policy rules, cross-DataSource metrics, or a visual
  authoring UI. Those remain later increments.
