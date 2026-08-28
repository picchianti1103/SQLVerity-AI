# ADR-009: Deterministic FinOps and execution-cost governance

- **Status:** Accepted for the MVP baseline
- **Date:** 2026-08-09

## Context

Token counts alone are insufficient for cost governance because provider/model prices change over
time, cached input can have a different rate, and estimates must be checked before incurring cost.
Database execution has a separate risk: syntactically safe read-only SQL can still be operationally
expensive. Hardcoding either model prices or planner thresholds in application code would make audit
history irreproducible and tenant policy difficult to change.

## Decision

Store tenant-scoped model-pricing records with half-open validity intervals, currency, token unit,
input/cached-input/output rates, optional batch discount, source version, and notes. Reject
overlapping records for the same tenant/provider/model. SQLite serializes the application-level
overlap check; PostgreSQL uses exclusion constraints. Normalize SQLite validity timestamps to UTC
and use `Decimal` for every model-cost calculation.

Before an LLM call, ask the provider for a token estimate and use the provider-declared model id to
resolve effective pricing. If a monthly tenant budget is active, missing model identity or missing
pricing blocks the call. A priced estimate is compared with actual or estimated usage already
recorded in that currency for the current UTC month. After a successful call, recalculate cost from
the provider's actual token counts and persist both estimated and actual cost with currency and the
pricing-record id. Never invent a price when no registry entry applies.

Expose tenant endpoints to create/list prices and budgets and to read a monthly usage summary grouped
by provider, model, and purpose. Budgets and currencies are not silently converted.

For database cost, store one optional policy per DataSource. Approval can require a prior `EXPLAIN`
and can reject planner total cost or estimated rows above configured thresholds. Missing planner
values fail closed whenever the corresponding threshold is configured. `EXPLAIN` itself remains
non-`ANALYZE` so governance does not execute the candidate query.

## Consequences

- Pricing changes remain attributable to a source version and historical usage points to the exact
  pricing record used.
- Budget checks happen before provider generation whenever a budget is active and sufficient pricing
  metadata exists; missing metadata is a denial, not a zero-cost assumption.
- Actual provider cost can differ from the estimate, so the recorded actual value is authoritative
  for subsequent monthly budget checks.
- Tenant budgets are monthly and single-currency in this increment. Project/user attribution,
  currency conversion, alerts, and carry-over are deferred.
- Scenario simulation, provider/model recommendations, automatic price-list ingestion, and a FinOps
  frontend remain separate increments.
- PostgreSQL deployment requires the standard `btree_gist` extension for race-safe interval
  exclusion constraints; this cannot be integration-tested until a live PostgreSQL fixture exists.
