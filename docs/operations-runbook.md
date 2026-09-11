# Operations runbook

SQLVerity AI exposes liveness at `/health`, dependency readiness at `/health/ready`, and
authenticated Prometheus metrics at `/v1/system/metrics`. Configure the scraper with a
platform-admin bearer credential held by the monitoring secret store. Never place that
credential in the Prometheus configuration repository.

OpenTelemetry is opt-in. Set `SQLVERITY_OTEL_ENABLED=true`, an HTTPS
`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, and a bounded
`SQLVERITY_OTEL_TRACE_SAMPLE_RATIO`. The exporter uses batched OTLP/HTTP spans and W3C trace
context. Spans and structured request logs contain only request ID, trace ID, HTTP method,
templated route, status, and duration; they exclude URLs, query strings, identities, prompts,
SQL, and database results.

Load `deploy/observability/prometheus-alerts.yml` into Prometheus after adjusting thresholds
to the measured service-level objectives. The readiness alert expects a blackbox probe named
`sqlverity-readiness` pointed at `/health/ready`.

## Readiness

Inspect the readiness JSON to distinguish catalog and worker failures. For catalog failures,
verify secret resolution, TLS, PostgreSQL reachability, pool exhaustion, and migration state.
For worker failures, restart only after checking the last worker log for a bounded error type;
leased jobs become reclaimable after `SQLVERITY_BACKGROUND_JOB_LEASE_SECONDS`.

PostgreSQL deployments also require the same random `SQLVERITY_PREFLIGHT_SIGNING_KEY` (at least 32
bytes) on every API replica. Startup fails if it is absent. If confirmations are unexpectedly
rejected, verify clock synchronization, the bounded `SQLVERITY_PREFLIGHT_TTL_SECONDS`, the shared key,
and catalog access to `ai_preflight_confirmations`; never mark a nonce unconsumed manually. Use the
read-only AI transfer receipt endpoint and usage-event link for investigation without exporting
prompt content.

## Server errors

Use `X-Request-ID` and `traceparent` to correlate the request, centralized JSON log, and trace.
Compare the first affected deployment and provider circuit state. Roll back an application or
migration only using its documented procedure; do not edit catalog rows manually.

## Latency

Split the histogram by templated route. Check catalog pool saturation, provider latency,
database execution time, and background work. Lowering trace sampling does not correct
application latency.

## Throttling

Identify whether user, tenant, or DataSource limits are saturated from the structured quota
responses. A process crash can orphan a request lease until its TTL expires. Changing rate windows
does not release active requests; late or repeated releases affect only their original lease ID.
Check lease renewal and expiry alongside traffic before diagnosing persistent concurrency saturation.
Increase limits only after confirming database and provider capacity.

## Background worker

Confirm at least one deployment replica has `SQLVERITY_BACKGROUND_WORKER_ENABLED=true` and that
the thread is alive. Multiple replicas may safely claim jobs through catalog leases. Do not
delete running jobs; stop the worker and let the lease expire before recovery. A successful batch
and its continuation are committed in one catalog transaction, so recovery should inspect job state
rather than enqueueing a duplicate continuation manually.

## Upgrade and rollback

Catalog migrations are forward-only and serialized by a PostgreSQL advisory lock. Before deploying
a revision with new migrations, take and verify a catalog backup, complete an isolated restore
drill, and follow [migration-and-rollback.md](migration-and-rollback.md). Rollback means restoring the
pre-upgrade backup into a controlled target and redeploying the compatible application; never remove
rows from `sqlverity_schema_migrations` or edit catalog tables manually.

## Audit export and incident handling

Export tenant audit events through `/v1/tenants/{tenant_id}/audit/export` with an audit-reader
role and send the response directly to immutable object storage. Exports intentionally avoid
credentials, prompt bodies, SQL text, and result data. Preserve correlated infrastructure logs
under the organization retention policy and follow the security contact in `SECURITY.md`.

## Request leases and uncertain LLM charges

`SQLVERITY_REQUEST_LEASE_SECONDS` defaults to 120 (allowed 3–3600). API replicas renew a
request's user, tenant, and DataSource leases every third of that duration until the complete
ASGI response finishes. Rate-window rollover does not free capacity. A crashed worker's lease
expires; late and duplicate releases cannot affect a replacement request. Keep clocks synchronized
and select a TTL with margin for catalog latency and event-loop stalls. Renewal failure stops the
HTTP operation (503 before headers; interruption after headers). Cancellation is cooperative and
cannot undo an external operation already accepted by a database or provider; their timeouts remain
necessary. Expired leases are cleaned during acquisition and cascade with old inactive quota windows
through operational retention.

LLM budget reservations have no automatic expiry: a provider timeout can still incur a charge.
With a principal permitted to manage FinOps, inspect
`GET /v1/tenants/{tenant_id}/finops/reservations`. The summary exposes `reserved_cost`,
`uncertain_cost` (a subset of reserved), and `reconciled_cost` (included in total cost).
Available budget subtracts both recorded and reserved amounts in the reservation's UTC month.

For an uncertain response, confirm final billing with the provider. For an abandoned `reserved`
or `in_flight` record, stop/confirm termination of its worker first; reconciliation permits those
states only after one hour without an update. Then call
`POST /v1/tenants/{tenant_id}/finops/reservations/{reservation_id}/reconcile` with, for example:

```json
{"actual_cost": "0.0012", "reason": "Provider invoice/request reference confirms final charge"}
```

Use zero only with evidence of no charge. The amount uses the reservation currency; the server
records the authenticated actor, reason, and amount in audit. Repeated or live-state reconciliation
returns 409, and another tenant's reservation returns 404. Charges confirmed above the estimate are
recorded in full and reduce future capacity; an estimate is not a guarantee of the provider invoice.
