# Configuration reference

This page collects the setup details intentionally kept out of the main README. The committed
[`.env.example`](../.env.example) file is the source of truth for available environment variables
and safe development defaults.

## Runtime modes

The Docker Compose setup uses PostgreSQL for the SQLVerity AI metadata catalog and also creates a separate
synthetic demo database. Native startup uses `sqlverity_catalog.sqlite3` by default.

To use PostgreSQL as the catalog in a native deployment, configure:

```powershell
$env:SQLVERITY_CATALOG_BACKEND='postgresql'
$env:SQLVERITY_CATALOG_SECRET_REF='env://SQLVERITY_CATALOG_DB'
$env:SQLVERITY_CATALOG_DB='{"host":"localhost","port":5432,"database":"sqlverity","username":"sqlverity","password":"...","sslmode":"require"}'
```

Catalog migrations are packaged under [`migrations/postgresql`](../migrations/postgresql). Follow
the [migration and rollback guide](migration-and-rollback.md) when upgrading an existing catalog;
do not delete a populated Compose volume to apply an upgrade.

## Authentication

Startup fails closed unless `SQLVERITY_BOOTSTRAP_API_KEY` contains at least 32 characters. The bootstrap
credential is intended only for initial administration. Send it as a Bearer token, create a tenant,
then provision normal scoped credentials through the security API or web console.

Issued API keys are displayed once and stored only as hashes. Production deployments should load
the bootstrap credential and all other secrets from a secret manager.

Optional OIDC configuration is documented directly in [`.env.example`](../.env.example). When it
is enabled, the browser uses Authorization Code with PKCE, an HttpOnly session, and CSRF protection.

## Optional installation extras

The minimal web and API installation is:

```powershell
python -m pip install -e .
```

Install only the integrations needed by the deployment:

| Extra | Purpose |
|---|---|
| `postgres` | PostgreSQL catalog and DataSource support |
| `mysql` | MySQL support |
| `oracle` | Oracle support |
| `sqlserver` | SQL Server support |
| `openai` | Official OpenAI SDK |
| `identity` | OIDC token verification |
| `secrets` | AWS Secrets Manager |
| `observability` | OTLP/HTTP trace export |
| `dev` | Complete offline development and verification toolchain |
| `all` | All currently distributable runtime integrations |

For example:

```powershell
python -m pip install -e ".[dev,postgres,openai]"
```

MariaDB remains supported by the runtime adapter, but its packaging extra is withheld while the
available Python driver release is affected by `PYSEC-2026-217`. Install only an
organization-approved patched driver.

## LLM providers

LLM access is disabled by default. Credentials alone never enable a model call. Select either one
provider with `SQLVERITY_LLM_PROVIDER` or several with `SQLVERITY_LLM_PROVIDERS`; do not set both variables.

### Local Ollama

```powershell
$env:SQLVERITY_LLM_PROVIDER='ollama'
$env:SQLVERITY_OLLAMA_MODEL='your-approved-local-model'
```

Native startup uses `http://127.0.0.1:11434` by default. Compose reaches a host Ollama instance
through `host.docker.internal`. A non-loopback private endpoint requires explicit remote opt-in and
HTTPS; see [`.env.example`](../.env.example).

### OpenAI

```powershell
$env:SQLVERITY_LLM_PROVIDER='openai'
$env:OPENAI_API_KEY='load-from-your-secret-manager'
$env:SQLVERITY_OPENAI_MODEL='your-approved-model-id'
```

### Multiple cloud providers

```powershell
$env:SQLVERITY_LLM_PROVIDERS='openai,anthropic,gemini,kimi'
$env:OPENAI_API_KEY='load-from-your-secret-manager'
$env:SQLVERITY_OPENAI_MODEL='your-approved-openai-model-id'
$env:ANTHROPIC_API_KEY='load-from-your-secret-manager'
$env:SQLVERITY_ANTHROPIC_MODEL='your-approved-claude-model-id'
$env:GEMINI_API_KEY='load-from-your-secret-manager'
$env:SQLVERITY_GEMINI_MODEL='your-approved-gemini-model-id'
$env:MOONSHOT_API_KEY='load-from-your-secret-manager'
$env:SQLVERITY_KIMI_MODEL='your-approved-kimi-model-id'
```

`claude` is accepted as an alias for `anthropic`, and `moonshot` as an alias for `kimi`. Every
selected provider needs both its credential and exact model ID. Unknown, duplicate, empty, or
incomplete selections stop startup. Startup and health checks never contact a model provider.

Timeout, output-token, residency, retention, and deployment-type settings for every provider are
listed in [`.env.example`](../.env.example).

## Provider egress policy

Provider selection is not sufficient: `SQLVERITY_REQUIRE_PROVIDER_POLICY` defaults to `true`. A security
administrator must define a tenant or DataSource policy that allows the required purposes and
classifications and matches the configured residency and retention claims.

The supported purposes are:

- `sql_proposal_generation`
- `semantic_description_inference`
- `intent_correction_interpretation`

Production PostgreSQL deployments must also share a random `SQLVERITY_PREFLIGHT_SIGNING_KEY` of at
least 32 bytes across every API replica. It protects short-lived, single-use AI transfer
confirmations. `SQLVERITY_PREFLIGHT_TTL_SECONDS` defaults to 300 seconds.

## DataSource secrets

SQLVerity AI stores opaque secret references, never database passwords. For local development, put the
connection payload in an environment variable and use an `env://VARIABLE_NAME` reference when
creating the DataSource.

### PostgreSQL

```powershell
$env:SQLVERITY_ANALYTICS_DB='{"host":"localhost","port":5432,"database":"analytics","username":"sqlverity_reader","password":"...","sslmode":"prefer"}'
```

Use `env://SQLVERITY_ANALYTICS_DB` as the connection reference.

### MySQL or MariaDB

```powershell
$env:SQLVERITY_MYSQL_DB='{"host":"localhost","port":3306,"database":"analytics","username":"sqlverity_reader","password":"...","tls_required":true,"ssl_ca":"C:\\certs\\ca.pem"}'
```

### Oracle

```powershell
$env:SQLVERITY_ORACLE_DB='{"host":"oracle.internal","service_name":"analytics","username":"sqlverity_reader","password":"...","tls_required":true,"wallet_location":"C:\\oracle-wallet","wallet_password":"..."}'
```

### SQL Server

```powershell
$env:SQLVERITY_SQLSERVER_DB='{"host":"sqlserver.internal","database":"analytics","username":"sqlverity_reader","password":"...","encrypt":true,"trust_server_certificate":false}'
```

Use a least-privileged, read-only database identity for every DataSource. Production deployments can
enable Vault or AWS Secrets Manager through `SQLVERITY_SECRET_BACKENDS` and use `vault://...` or
`aws-secretsmanager://...` references. Secrets are resolved for each connection so rotation does
not require catalog changes.

## Production operations

Before a production release, review:

- [Operations runbook](operations-runbook.md)
- [Certification matrix](certification-matrix.md)
- [Live certification](live-certification.md)
- [Backup and restore](backup-and-restore.md)
- [Retention](retention.md)
- [Load testing](load-testing.md)

The project is a developer preview. Passing the offline test suite does not replace live validation
against the exact database, provider, identity, secret-management, and deployment configuration.
