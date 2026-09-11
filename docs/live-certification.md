# Live certification

SQLVerity AI keeps offline contract tests separate from tests that contact real databases or models.
CI invokes the PostgreSQL job in `Live certification` on pushes and pull requests. Its provider
job runs only through an explicit manual dispatch with selected providers.

## PostgreSQL integration

The workflow starts PostgreSQL 17, loads `fixtures/live/postgresql_golden.sql`, applies every
catalog migration through `PostgreSQLCatalogRepository`, and verifies real introspection,
`EXPLAIN`, read-only execution, result bounds, catalog readiness, physical identifier identity,
and concurrent EXPLAIN/approval revision binding through independent catalog connections.

Run the same test against a disposable local instance by setting an opaque reference:

```powershell
$env:SQLVERITY_SECRET_BACKENDS='environment'
$env:SQLVERITY_LIVE_POSTGRES_SECRET_REF='env://SQLVERITY_LIVE_POSTGRES'
$env:SQLVERITY_LIVE_POSTGRES='{"host":"127.0.0.1","port":5432,"database":"sqlverity_live","username":"sqlverity","password":"...","sslmode":"prefer"}'
psql -d sqlverity_live -f fixtures/live/postgresql_golden.sql
python -m pytest -q tests/live/test_postgresql_live.py
```

## Console browser regressions

The browser suite starts the real API on loopback with a temporary SQLite catalog, synthetic
tenants, and scoped credentials. It checks analyst/viewer access and out-of-order schema responses
across context changes. No provider credentials or model calls are required. Install the `dev`
extra and a Playwright browser before opting in:

```powershell
python -m pip install -e ".[dev]"
python -m playwright install chromium
node --test tests/web/console.test.cjs
$env:SQLVERITY_RUN_BROWSER_TESTS='true'
python -m pytest -q tests/browser/test_console_browser.py
```

Node 22 or newer runs the deferred-response suite without npm packages. On a Windows machine with
Microsoft Edge installed, set `SQLVERITY_BROWSER_CHANNEL=msedge` to use that browser instead of
installing Chromium. CI installs Chromium and its Linux system dependencies, then runs both suites.
The Node tests inject late successes and errors for schema/privacy/preflight/query work, OIDC,
reconnection, logout, and parameter changes; the browser suite covers real DOM/API integration.

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

## September review regressions

The PostgreSQL job also runs the shared two-repository budget, lease, and approval/policy cases.
These use synthetic providers and verify admission/settlement races without paid API calls.
On 2026-09-11, PostgreSQL 17.11 on Windows passed 27 tests and 3 subtests, including physical
identifier/search-path and duplicate-output checks. The same disposable runtime previously passed
migrations through `0018` on a fresh database. The console passed 12 Node tests and three browser
tests on Microsoft Edge with the real local API and synthetic data.
This evidence covers the local PostgreSQL tests; CI execution and real-provider accuracy are
separate checks. See [remediation status](review-remediation-2026-09.md).
