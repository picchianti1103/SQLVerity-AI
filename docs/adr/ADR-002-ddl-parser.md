# ADR-002: PostgreSQL DDL parsing strategy

- **Status:** Accepted for the MVP
- **Date:** 2026-08-09

## Decision

Use SQLGlot 30.x as the PostgreSQL DDL AST adapter. Keep SQLGlot nodes inside the connector
package and translate them immediately into provider-neutral `DataSourceSnapshot` objects.

DDL input is never executed. The first supported subset is:

- `CREATE SCHEMA` as namespace context;
- `CREATE TABLE` with columns, physical types, nullability, defaults, primary keys, and foreign
  keys;
- `CREATE VIEW` and materialized views represented as governed view objects;
- `COMMENT ON TABLE` and `COMMENT ON COLUMN` as `IMPORTED` semantic evidence.

Any other statement fails closed. Duplicate objects or columns, ambiguous target catalogs,
unresolved foreign keys, and invalid relationship columns are rejected before creating a catalog
version.

## Consequences

- The domain and catalog do not depend on SQLGlot expression classes.
- The SQLGlot major version is constrained because its AST is an adapter contract.
- Supporting broader database dumps will require explicit handlers and regression fixtures rather
  than silently ignoring unknown DDL.
