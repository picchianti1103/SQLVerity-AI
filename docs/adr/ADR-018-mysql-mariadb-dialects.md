# ADR-018: Dialect registry and MySQL/MariaDB execution adapters

- **Status:** Accepted
- **Date:** 2026-08-16

## Context

The first executable vertical slice was intentionally PostgreSQL-only. Dialect selection was stored
on each DataSource, but validation, DDL import, semantic SQL fragments, API wiring, revalidation,
introspection, and execution still contained PostgreSQL-specific branches or defaults. Accepting a
different dialect string therefore did not mean that the database was supported safely end to end.

MySQL and MariaDB share SQL and wire-protocol ancestry, but they differ in official Python drivers
and statement-timeout variables. Treating them as one indistinguishable runtime would make driver
compatibility and timeout enforcement implicit.

## Decision

Introduce a central registry of immutable `DialectCapabilities`. The registry canonicalizes aliases,
maps each dialect to its SQLGlot reader/writer, and fails closed for unregistered dialects. Register
PostgreSQL (`postgres` alias), MySQL, and MariaDB. A validator registry routes each proposal to an
explicit dialect validator; there is no PostgreSQL fallback.

Add MySQL and MariaDB DDL parsing, governed metric/rule fragment parsing, manual/DDL catalog import,
information-schema introspection, non-`ANALYZE` JSON `EXPLAIN`, server-enforced read-only
transactions, statement timeouts, bounded streaming fetches, and active cancellation through a
separate `KILL QUERY <connection_id>` connection. MySQL uses `MAX_EXECUTION_TIME` in milliseconds;
MariaDB uses `max_statement_time` in seconds.

Use each vendor's official driver:

- MySQL Connector/Python for MySQL;
- MariaDB Connector/Python for MariaDB.

Connection credentials continue to resolve only at runtime from an opaque secret reference. TLS is
enabled by default. Supplying a CA enables certificate verification; production deployments should
always provide the appropriate CA rather than relying on encryption without identity verification.

DataSource creation canonicalizes and validates the dialect immediately. The specialized Authorized
Query DataSource remains PostgreSQL-only because its parameter compiler and virtual-surface wrapper
have a separate dialect contract; attempting to create that source type with MySQL or MariaDB fails
early and explicitly.

## Consequences

- Direct, manual, hybrid, and DDL-import DataSources can use PostgreSQL, MySQL, or MariaDB through
  the same governed catalog/generation/execution lifecycle.
- Corrected SQL and governed metric/rule fragments are parsed with the owning DataSource dialect.
- Adding another dialect requires capabilities plus explicit acquisition/execution adapters; a name
  alone can no longer imply support.
- Static and injected-connection tests cover both new dialects. Live server interoperability,
  permissions, TLS chains, optimizer-plan variants, and cancellation remain an external integration
  gate until disposable MySQL and MariaDB services are available.

## Vendor references

- MySQL Connector/Python installation and API:
  <https://dev.mysql.com/doc/connector-python/en/quick-installation-guide.html>
- MySQL read-only transaction API:
  <https://dev.mysql.com/doc/connector-python/en/connector-python-api-mysqlconnection-start-transaction.html>
- MySQL `KILL QUERY`:
  <https://dev.mysql.com/doc/refman/8.4/en/kill.html>
- MariaDB Connector/Python connection API:
  <https://mariadb.com/docs/connectors/mariadb-connector-python/api/connection>
- MariaDB statement timeouts:
  <https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/query-optimizations/query-limits-and-timeouts>
- MariaDB `KILL QUERY`:
  <https://mariadb.com/docs/server/reference/sql-statements/administrative-sql-statements/kill>

