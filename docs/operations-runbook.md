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
logs and audit trail. A process crash can orphan an in-memory request, so concurrency counters are
reset when the next configured request window begins; a late release from the old window cannot
decrement the new counter. Repeated saturation within one window is real load, not a stale lease.
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
