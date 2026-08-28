# ADR-019: Ollama local provider and Oracle/SQL Server adapters

- **Status:** Accepted
- **Date:** 2026-08-16

## Context

The provider-neutral gateway had a concrete cloud adapter but no concrete local/private runtime,
leaving one explicit MVP capability open. Direct database support covered PostgreSQL, MySQL, and
MariaDB, while Oracle and Microsoft SQL Server required different catalog APIs, driver contracts,
planner surfaces, timeout mechanisms, and cancellation behavior.

Treating either problem as a name-only configuration change would bypass established fail-closed
boundaries. The local provider still needs strict structured output and usage telemetry. Each
database needs a real SQL parser dialect, introspection adapter, read-only enforcement, bounded
execution, a non-executing plan path, and explicit cancellation semantics.

## Decision

Add an explicit `ollama` provider using Ollama's `/api/chat` structured-output contract. The adapter
sends the JSON Schema both as `format` and in trusted system instructions, sets temperature to zero,
disables streaming, validates completion/model/JSON/token telemetry, and applies the same
conservative pre-call token budget estimate used at the gateway boundary. It performs no network
call during startup or health checks. Loopback HTTP is allowed by default; non-loopback endpoints
require `SQLVERITY_OLLAMA_ALLOW_REMOTE=true` and HTTPS. Optional bearer credentials are held only in
memory and hidden from representations.

Extend the central dialect and validator registries with:

- Oracle, using SQLGlot's `oracle` reader/writer;
- SQL Server, using SQLGlot's `tsql` reader/writer and aliases `mssql`, `sqlserver`, and `tsql`.

Both dialects receive DDL/manual import and explicit API routing. Unknown and non-allowlisted
functions continue to fail closed.

Use `python-oracledb` 4.x in its default Thin mode. Oracle connections use TCPS and server identity
matching by default, with explicit opt-out only for controlled development. Introspection reads the
connected user's `USER_*` catalog in a read-only transaction. Execution sets `call_timeout`, starts
a read-only transaction, bounds rows and serialized bytes, rolls back, and cancels through
`Connection.cancel()`. Estimated plans use `EXPLAIN PLAN` plus `DBMS_XPLAN.DISPLAY`; the internal
`PLAN_TABLE` write is rolled back and the user SQL is not executed.

Use Microsoft's `mssql-python` driver. Credentials are passed as normalized keyword arguments,
encryption and certificate verification are enabled by default, and `ApplicationIntent=ReadOnly` is
declared. The adapter also sets `SQL_ATTR_ACCESS_MODE=SQL_MODE_READ_ONLY`. Query timeout is applied
through `Connection.timeout` before cursor creation. Estimated plans use `SET SHOWPLAN_XML ON`, which
does not execute the query. Active session ids come only from `SELECT @@SPID`; cancellation issues
`KILL <validated integer>` through a separate connection. ADR-022 adds governed generated-query
parameters: canonical named placeholders are converted through the validated AST to ordered `?`
bindings before `mssql-python` receives the statement and values.

The specialized Authorized Query DataSource remains PostgreSQL-only. Its virtual-surface compiler,
placeholder syntax, and lifecycle signature require a separate per-dialect design.

## Consequences

- The explicit MVP provider scope is complete: deployments can choose the OpenAI cloud adapter or
  an Ollama local/private adapter, and neither is enabled implicitly.
- Direct, manual, DDL-import, and hybrid catalog flows now recognize PostgreSQL, MySQL, MariaDB,
  Oracle, and SQL Server.
- Oracle introspection is intentionally limited to objects visible through the connected user's
  `USER_*` views. Broader cross-schema acquisition requires separately governed credentials and
  catalog policy.
- Oracle plans require `PLAN_TABLE`/`DBMS_XPLAN` access. SQL Server plans require `SHOWPLAN` and
  cancellation requires permission to terminate the target session. These permission contracts
  must be verified against disposable live services.
- SQL Server `KILL` cancellation assumes the control connection reaches the same SQL Server instance
  as the active session. Availability-group routing therefore remains a live integration concern.
- Static, protocol, AST, and injected-connection tests cover the new paths without persisting
  credentials or sending prompts/queries to external services.

## Vendor references

- Ollama structured outputs: <https://docs.ollama.com/capabilities/structured-outputs>
- Ollama chat API: <https://docs.ollama.com/api/chat>
- Ollama usage fields: <https://docs.ollama.com/api/usage>
- python-oracledb connection handling:
  <https://python-oracledb.readthedocs.io/en/stable/user_guide/connection_handling.html>
- python-oracledb connection API:
  <https://python-oracledb.readthedocs.io/en/stable/api_manual/connection.html>
- Microsoft `mssql-python` driver:
  <https://learn.microsoft.com/en-us/sql/connect/python/mssql-python/python-sql-driver-mssql-python?view=sql-server-ver17>
- Microsoft connection management:
  <https://learn.microsoft.com/en-us/sql/connect/python/mssql-python/connection-management?view=sql-server-ver17>
- SQL Server SHOWPLAN XML:
  <https://learn.microsoft.com/en-us/sql/relational-databases/performance/save-an-execution-plan-in-xml-format?view=sql-server-ver17>
