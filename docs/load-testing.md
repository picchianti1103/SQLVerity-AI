# Load testing and performance evidence

`sqlverity-load-test` runs a bounded, GET-only concurrent profile and reports throughput, p50/p95/p99
latency, server-error rate, throttle rate, and status counts. It does not retain response bodies,
follow redirects, accept query strings, or print credentials. Pass an optional bearer credential
only through `SQLVERITY_LOAD_TEST_BEARER_TOKEN`.

```console
SQLVERITY_LOAD_TEST_BEARER_TOKEN=... sqlverity-load-test \
  --base-url https://sqlverity.example.com \
  --path /v1/system/capabilities \
  --requests 1000 \
  --concurrency 25 \
  --maximum-p95-ms 2000 \
  --write-report load-report.json
```

Plain HTTP is accepted automatically only for loopback. `--allow-insecure` is an explicit option
for a disposable private test environment. Request count and concurrency have hard upper bounds.
The command exits non-zero when p95 latency, transport failures, non-success HTTP responses, or 429
responses exceed their configured thresholds. `429` is reported separately as throttling; all other
non-2xx responses—including authentication and redirect responses—count as errors. Only exact
`/health`, `/health/ready`, or `/v1/...` paths are accepted.

The public CI container job runs a small deterministic profile. Release certification must use a
production-shaped, isolated deployment and preserve the JSON report with replica count, CPU/memory,
catalog size, database/provider versions, quota settings, and source revision. Load tests must not
target production without a separately approved change window.
