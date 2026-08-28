# Third-party notices

SQLVerity AI's own source code is licensed under Apache-2.0. Its dependencies are separate works and retain
their own copyright notices and license terms. This inventory records the direct runtime and optional
dependencies observed during the `0.1.0` release audit on 2026-08-17; it is not a lockfile, a complete
transitive inventory, or legal advice. Verify the exact distributions used in every release image.

| Component | Audited version | Integration | Reported license / notable terms |
|---|---:|---|---|
| FastAPI | 0.141.1 | Core | MIT |
| HTTPX | 0.28.1 | Core | BSD-3-Clause |
| Pydantic | 2.13.4 | Core | MIT |
| SQLGlot | 30.15.0 | Core | MIT |
| Uvicorn | 0.52.1 | Core | BSD-3-Clause |
| OpenAI Python | 2.54.0 | `openai` extra | Apache-2.0 |
| PyJWT | 2.x constraint | `identity` extra | MIT |
| Boto3 | 1.x constraint | `secrets` extra | Apache-2.0 |
| OpenTelemetry API, SDK, and OTLP HTTP exporter | 1.x constraint | `observability` extra | Apache-2.0 |
| Psycopg / psycopg-binary | 3.3.4 | `postgres` extra | LGPL-3.0-only |
| MySQL Connector/Python | 9.7.0 | `mysql` extra | GPL-2.0 with additional permissions and the Universal FOSS Exception 1.0; review the installed distribution's complete terms |
| python-oracledb | 4.0.2 | `oracle` extra | UPL-1.0 OR Apache-2.0 |
| mssql-python | 1.13.0 | `sqlserver` extra | Python package code reports MIT; bundled Windows DLLs carry separate Microsoft terms in the installed distribution |

The MariaDB adapter dynamically loads the official `mariadb` package, but SQLVerity AI does not currently
declare or bundle that package as an installation extra. The latest public release available during
this audit was affected by `PYSEC-2026-217`, with no corrected PyPI release available. Re-enable the
extra only after reviewing an upstream fix and the exact connector/native-library combination.

For authoritative terms, inspect each installed package's `METADATA`, `LICENSE*`, and `NOTICE*` files
and the corresponding upstream source release. Package metadata and vendor files take precedence over
this convenience summary.
