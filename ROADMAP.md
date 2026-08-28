# Roadmap

SQLVerity AI is a developer preview. The near-term goal is not to maximize feature count; it is to prove
that governed AI-assisted SQL solves a painful problem for identifiable users.

This roadmap describes priorities, not delivery promises. Security and correctness issues can reorder it.

## Now — prove the first-use path

- Keep the Docker quickstart reproducible from a clean clone.
- Reach ten independent evaluators and document where they abandon or distrust the workflow.
- Make first catalog value achievable in under 15 minutes on a supported local setup.
- Certify PostgreSQL end to end with repeatable live tests and publish the evidence.
- Distribute signed or attestable Python and container artifacts through repeatable release workflows.
- Turn repeated early-adopter friction into small, testable changes.

Exit signal: at least three external teams can describe a credible pilot and two complete the governed
proposal-to-result path without maintainer intervention.

## Next — validate real operating boundaries

- Expand live certification for MySQL/MariaDB, Oracle, and SQL Server according to user demand.
- Add deployment examples for a production identity provider and external secret manager.
- Improve the semantic review loop using measured correction cases, not synthetic feature breadth.
- Publish performance envelopes for representative schema sizes and concurrent read-only workloads.
- Reduce installation and upgrade friction based on observed pilot failures.

Exit signal: a time-bounded pilot can define, observe, and reproduce its security, quality, and latency
acceptance criteria.

## Later — scale what users have proven

- Deeper governed metric and business-concept workflows.
- Administration and audit views for larger multi-tenant deployments.
- Additional deployment targets and provider certification.
- Carefully scoped multi-turn clarification where it improves measured correctness without obscuring
  approval boundaries.

## Explicit non-goals

- Executing write statements or autonomous database changes.
- Sending database rows, credentials, raw bound parameters, query plans, or results to an LLM.
- Hiding generated SQL or removing explicit approval from the execution path.
- Adding connectors or providers solely to make the compatibility list longer.
- Cross-DataSource joins before isolation, semantics, and cost behavior can be made explicit.

## How priorities are chosen

Work is ranked by security impact, correctness impact, repeated user evidence, and the shortest path to a
credible pilot. A request with a synthetic reproduction and a measurable outcome carries more weight
than a broad request without a user or acceptance criterion.

See the [early-adopter guide](docs/early-adopter-guide.md) to provide that evidence.
