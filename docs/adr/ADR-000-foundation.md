# ADR-000: Foundation boundaries

- **Status:** Accepted for the first implementation slice
- **Date:** 2026-08-08

## Context

The product document makes semantic governance, tenant isolation, provider independence, and
preview-before-execution foundational constraints. Starting with an LLM integration would make
those constraints incidental instead of architectural.

## Decision

Use a modular monolith with a dependency-free domain core. Define provider, connector, and policy
ports before implementing adapters. Keep semantic evidence immutable and maintain a separate
current resolution. A lower-authority inference never overwrites confirmed knowledge; equal
authority with different meaning creates an explicit conflict.

PostgreSQL is the production catalog target. SQLite is an intentionally replaceable local/test
adapter so the domain rules can be executed without external infrastructure.

The initial query lifecycle is preview-first: execution is unreachable until validation has
produced `READY_FOR_PREVIEW` and an explicit transition to `APPROVED` occurs.

## Consequences

- Core rules can be tested without database or LLM services.
- Every repository read includes the tenant boundary.
- Evidence history and current semantic truth are separate concepts.
- Connector, SQL parser, LLM, and PostgreSQL repository adapters can be added independently.

