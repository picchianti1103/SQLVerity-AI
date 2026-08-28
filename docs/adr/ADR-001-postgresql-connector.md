# ADR-001: PostgreSQL connector baseline

- **Status:** Accepted for the MVP
- **Date:** 2026-08-08

## Decision

Support PostgreSQL 16 or newer for the MVP. Version 16 remains supported through November 2028,
providing a conservative compatibility floor without targeting a near-EOL release.

Use Psycopg 3 behind an injected connection factory. Production resolves credentials through a
`SecretResolver`; only the opaque secret reference is stored in the catalog. Password fields are
excluded from object representations.

Metadata ingestion runs inside an explicitly read-only transaction and reads `pg_catalog` for:

- ordinary, partitioned, foreign, materialized-view, and view relations;
- formatted column types, nullability, defaults, and comments;
- ordered primary-key columns;
- ordered source and target columns for foreign keys;
- view definitions.

## Consequences

- Tests do not require a live PostgreSQL instance or installed Psycopg package.
- A real connector integration suite is still required before claiming production support.
- The database login must independently be configured with least-privilege, read-only access.

