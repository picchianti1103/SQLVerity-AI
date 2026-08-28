# Live certification

SQLVerity AI keeps offline contract tests separate from tests that contact real databases or models.
The `Live certification` workflow is manual so provider calls are never made, and never billed,
without an explicit operator action.

## PostgreSQL integration

The workflow starts PostgreSQL 17, loads `fixtures/live/postgresql_golden.sql`, applies every
catalog migration through `PostgreSQLCatalogRepository`, and verifies real introspection,
`EXPLAIN`, read-only execution, result bounds, and catalog readiness.

Run the same test against a disposable local instance by setting an opaque reference:

```powershell
$env:SQLVERITY_SECRET_BACKENDS='environment'
$env:SQLVERITY_LIVE_POSTGRES_SECRET_REF='env://SQLVERITY_LIVE_POSTGRES'
$env:SQLVERITY_LIVE_POSTGRES='{"host":"127.0.0.1","port":5432,"database":"sqlverity_live","username":"sqlverity","password":"...","sslmode":"prefer"}'
psql -d sqlverity_live -f fixtures/live/postgresql_golden.sql
python -m pytest -q tests/live/test_postgresql_live.py
```

## Provider contract calls

Set `SQLVERITY_RUN_LIVE_PROVIDER_TESTS=true`, explicitly select providers, and provide their approved
non-production model ids and credentials. The test performs one minimal structured-output call per
selected provider and verifies real usage telemetry. It does not run in ordinary CI.

Before certifying the product path, declare the exact deployment type, residency, and retention
metadata, configure an acknowledged tenant or DataSource policy for the single test purpose, and
set the shared `SQLVERITY_PREFLIGHT_SIGNING_KEY`. In the console, retain evidence that the SQL preflight
returned `provider_invoked=false`, then confirm the bound transfer once and retain the minimized
receipt plus usage-event id. Replaying that confirmation must return `stale_preflight` with no
second provider call. The low-level provider contract test above is intentionally separate and must
not be presented as evidence that this governed product flow passed.

## Execution accuracy

Generate a hash-bound prediction file for the 50-case golden dataset, load the deterministic live
fixture, then compare validated predictions with the curated reference results:

```powershell
sqlverity-live-certify `
  --dataset fixtures/questions/golden_v1.json `
  --predictions .artifacts/predictions.json `
  --secret-ref env://SQLVERITY_LIVE_POSTGRES `
  --minimum-execution-accuracy 0.90 `
  --write-report .artifacts/live-certification.json
```

The command executes only expected-accepted cases whose predictions pass the offline validator.
It reports execution accuracy, candidate latency p50/p95, truncation or execution failures, and a
non-zero exit status when the required accuracy is missed. Use a disposable, synthetic database and
a least-privilege read-only credential outside the migration test.

Record each supported combination in a release artifact with database/server version, driver,
provider, model id, deployment/residency/retention claims, policy and acknowledgement versions,
preflight/receipt evidence, prompt revision, dataset hash, execution accuracy, p95 latency, average
cost, timestamp, and reviewer. A combination is supported only after its live row is green;
everything else remains experimental.
