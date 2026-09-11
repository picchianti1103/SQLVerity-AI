# September 2026 review: remediation status

Three remediation cycles address all eight findings from the local review dated 2026-09-08.
The final cycle was verified on 2026-09-11. Production certification remains separate work.

## Physical SQL identity (P1, finding 1)

The validator resolves physical catalog names exactly. Unquoted SQL identifiers are
normalized to lowercase for PostgreSQL and uppercase for Oracle; quoted names retain
their spelling. DDL import uses the same rules so the catalog records physical names.
MySQL, MariaDB, and SQL Server require exact spelling until server-specific name and
collation settings are represented in the catalog.

Every accepted physical table is emitted with its schema, and physical columns are
bound to their table or alias. SQL output quotes identifiers using the selected dialect.
Ambiguous schemas, duplicate source aliases, ambiguous self-joins, mismatching column
schemas, and unresolved derived columns are rejected. Catalog references with embedded
periods in table/schema names cannot be proven by the current dotted-string contract
and remain unsupported.

PostgreSQL execution sets a transaction-local `search_path` of `pg_catalog, pg_temp`.
The normalized SQL carries the selected application schema explicitly. Output lineage
and result masking preserve exact case, including distinct `id` and `"ID"` outputs.

These choices follow PostgreSQL's documented [identifier
rules](https://www.postgresql.org/docs/17/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS)
and [schema resolution
rules](https://www.postgresql.org/docs/17/ddl-schemas.html#DDL-SCHEMAS-PATH).

## EXPLAIN and approval binding (P1, finding 2)

EXPLAIN is available only while a ticket is `ready_for_preview`. Each successful
completion inserts immutable revision metadata: the executed SQL digest, parameter
digest and names, estimates, elapsed time, and timestamp. Raw parameter values and
the database plan are not persisted in the revision or audit event.

The ticket update and revision insert share one transaction. The update compares both
the preview state and the revision read before the database call. A cancelled or
approved ticket, or another EXPLAIN that already advanced the revision, produces a
conflict without inserting a revision or an audit event for the rejected completion.

Approval atomically records its own SQL and parameter digests and the precise EXPLAIN
revision. The compare-and-swap also rejects an EXPLAIN that completed while approval
was being prepared. Execution compares the prepared SQL and bindings with the independent
approval snapshot before calling the executor.

### API and console compatibility

`POST .../query-requests/{id}/explain` now returns `revision`. Clients must echo the
revision they reviewed in the approval body, alongside the same parameter values:

```json
{
  "expected_explain_revision": 1,
  "parameters": {"order_id": 101}
}
```

An omitted revision defaults to `0`, which permits approval only when no EXPLAIN has
been saved and existing policy allows approval without one. Stale revisions return
HTTP 409. The bundled console sends the reviewed revision and discards EXPLAIN/approval
successes belonging to a replaced query ticket. The broader context guards described under
finding 8 also cover session, tenant, DataSource, question, and parameter changes.

### Migration

PostgreSQL migration `0017_query_approval_binding.sql` adds the revision history and
approval binding fields. SQLite initializes/upgrades the equivalent schema. History
rows reject updates and deletes on both backends.

Old approved tickets have no independent approval digest and cannot execute: regenerate,
review, and approve them again. Existing preview tickets should receive a fresh EXPLAIN.
Catalogs imported from DDL with previously incorrect physical case should be reimported
before regenerating their tickets. Drain old replicas before upgrading; old application
code must not continue writing query state after this migration.

## Transactional LLM budget (P1, finding 3)

Before a priced call, the gateway reserves its estimated cost in a database transaction. A shared
row lock for the tenant and currency serializes admissions across processes. Each reservation
belongs to one UTC accounting month, provider/model, and pricing version. Active reservations are
included in the balance; another request cannot reuse the same available funds. Budget creation
shares the account lock.

Usage insertion, audit, and reservation settlement commit together. Actual costs above the estimate
remain recorded; subsequent requests see that spend. A known failure before provider dispatch
releases the hold. A failure after dispatch leaves the hold uncertain, including an error writing
usage. Crash-abandoned holds do not expire automatically. The FinOps API permits explicit,
role-protected reconciliation with an authenticated actor and evidence reason. See the
[operations runbook](operations-runbook.md#request-leases-and-uncertain-llm-charges).

Pricing and accounting time are pinned before dispatch. A call completing in a later month settles
in its original month. Estimates can differ from final provider charges; this change prevents
concurrent reuse of budget, not unreported provider fees or estimation overruns.

## Unique SQL output names (P2, finding 4)

Validation rejects duplicate output names in SELECT projections, including CTEs and derived
tables. Multiple output expressions must have known names: columns or explicit aliases.
For example, `SELECT id, id + 101 AS id` is rejected; distinct aliases preserve both values.
A single unnamed expression remains supported. Quoted case-distinct names remain distinct.
The rule is deliberately conservative for internal projections even when an outer column list
could rename them.

The shared driver metadata and bounded-fetch helpers reject duplicate names before building
row dictionaries, including empty results. All five database executors use these helpers.
Drivers never silently rename colliding outputs. Existing ambiguous proposals must be regenerated
with distinct aliases; this cycle needs no additional database migration.

## Independent request leases (P2, finding 5)

Each admitted HTTP request owns a unique lease across user, tenant, and DataSource scopes.
Concurrency is counted from unexpired lease rows independently of the rate-window counter.
Renewal runs during the complete ASGI response, including its body, every TTL/3; the default TTL is
120 seconds. Release matches the lease ID, so an old or repeated release cannot free newer work.
Expired workers cannot renew or resurrect a lease. Partial acquisitions release earlier scopes.

The API stops an HTTP operation when renewal fails. Database/provider cancellation is cooperative;
external work already accepted is still subject to its own execution timeout. This is a bounded
crash-recovery lease, not a fencing mechanism for remote side effects. FinOps reservations remain
independent and are not released by an expired HTTP lease.

## Execution cost policy binding (P2, finding 6)

Every policy update increments its revision. Approval stores that revision (0 when there is no
policy). Execution rechecks the current policy and passes its revision to the conditional state
transition. Policy writes and approval/execution admission lock the same DataSource row, including
when a policy is first created. A policy update after execution admission does not retroactively
cancel work already admitted.

An obsolete approval is returned to preview with its approval metadata cleared; the service
requires explicit approval again. Restrictive thresholds can block that approval. Even a relaxed
policy change requires review again. Audit preserves earlier approvals. A tenant/source-scoped
GET of the ticket state lets the console refresh after HTTP 409 and re-enable review actions.

Migration `0018_budget_leases_and_execution_policy.sql` supplies these tables and revisions, with
matching SQLite initialization/upgrades. Drain all old replicas before upgrading; running mixed
accounting implementations defeats admission coordination. Backup/rollback instructions are in the
[migration guide](migration-and-rollback.md).

## Scoped session discovery (P2, finding 7)

Authenticated `GET /v1/session` returns a minimal principal view, effective permissions, and the
tenants/DataSources visible through current role assignments. A DataSource-scoped analyst can
discover and select that source without acquiring a tenant role or seeing sibling sources.
The response excludes credential material and connection secret references. Revoked API keys
cannot call the endpoint. Bootstrap authority can discover all tenants and sources.

The console uses this endpoint and enables actions according to the returned permissions.
Existing tenant/source listing and administration endpoints retain their authorization checks;
direct API access remains independently enforced. Discovery grants no new roles or permissions.

## Console context and session races (P2, finding 8)

The console tracks request generations for session, tenant, DataSource, and query contexts.
Replacing a context or repeating an operation cancels its previous requests. Every completion
checks ownership, including failures, so a late response cannot replace the current schema,
privacy state, preflight, query ticket, or connection. Each operation also owns its button state;
an old completion cannot unlock a button used by newer work.

Logout clears the local session immediately, and late OIDC startup or logout responses cannot
overwrite a newer connection. Question and parameter edits invalidate dependent query work.
Approved parameter fields stay locked to the reviewed bindings; a policy conflict returning the
ticket to preview makes them editable again. Context changes clear dependent results and forms.
Aborting a browser request does not undo an operation already accepted by the server.

Node tests deliberately deliver late successes and failures even after cancellation. Browser
tests exercise the real console and API with a synthetic SQLite catalog: scoped analyst access,
viewer restrictions, and a delayed schema failure after switching tenant and source. These tests
make no real model calls. CI now includes both Node regressions and Chromium browser tests.

## Verification and remaining work

Regression coverage includes exact identifiers, case-distinct output masking, CTE aliases,
concurrent EXPLAIN completion, immutable approval bindings, concurrent budget admissions, provider
failure, transactional settlement rollback, manual charges, UTC month boundaries, rollover,
renewal/expiry, duplicate release, and policy changes between service validation and database CAS.
The final cycle adds duplicate projection/driver metadata checks, scoped discovery and isolation,
and asynchronous browser context/session regressions.
SQLite races use separate connections to a temporary file; PostgreSQL uses separate connection pools.

On September 10, a disposable PostgreSQL 17.11 runtime passed 26 tests and 3 subtests with synthetic
data, including migrations on a fresh database. Those tests also exposed and fixed nullable-scope
parameter typing and boolean lineage persistence in the shared repository. CI invokes the same
PostgreSQL job on pushes and pull requests; real-provider certification remains manual.

Final September 11 checks: 335 tests and 93 subtests passed. The ordinary run skipped 31 opt-in
cases: 27 PostgreSQL tests, three browser tests, and one live-provider test. PostgreSQL 17.11
passed all 27 tests and three subtests separately; the three browser tests passed on Microsoft
Edge with the real local API. All 12 Node regressions passed. Ruff and strict mypy passed
(176 source files), the golden gate passed 50/50 cases, and distribution build/Twine checks
passed. The wheel includes the console request coordinator and the new discovery/validation
code, as well as migrations `0017` and `0018`. The live-provider test was not run.

| Review item | Status after three cycles |
| --- | --- |
| 1. Physical identifier identity | Implemented; offline and live PostgreSQL checks passed |
| 2. EXPLAIN/approval/parameter race | Implemented; SQLite and PostgreSQL races passed |
| 3. Transactional LLM budget reservations | Implemented; concurrent admissions and settlement verified on both backends |
| 4. Duplicate output names | Implemented; five-dialect validation and real PostgreSQL driver checks passed |
| 5. Concurrency leases independent of rate windows | Implemented; rollover, renewal, expiry, and release verified |
| 6. Current DB cost policy before execution | Implemented; policy/approval/execution races verified on both backends |
| 7. Discovery for DataSource-scoped principals | Implemented; API isolation and scoped browser access passed |
| 8. Console context/session request races | Implemented; delayed success/failure regressions and browser context switching passed |

Full product-path/provider accuracy, broader refactoring, and production measurements remain
separate work; these regression checks do not establish production readiness.
