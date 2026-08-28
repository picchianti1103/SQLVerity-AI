# ADR-020: Self-service Control Plane and Query Studio

- **Status:** Accepted
- **Date:** 2026-08-16

## Context

SQLVerity AI exposed the governed workflow only through HTTP APIs. Initial setup required hand-authored
requests, and users could not inspect the catalog, SQL validation, plan, approval boundary, result
privacy, and provenance from one place. A separate frontend deployment would add a new build,
cross-origin authentication, version skew, and operational dependency before the interaction model
has stabilized.

The interface must not weaken the existing trust boundaries. In particular, it must not persist API
keys in browser storage, accept raw database passwords into the catalog, execute proposed SQL
directly, or duplicate policy decisions in client code.

## Decision

Ship a dependency-free HTML, CSS, and JavaScript console as package data beside the FastAPI
application and serve it at `/ui`. The shell is public because it contains no runtime or tenant data;
every data operation continues through authenticated `/v1` endpoints. Serve the document with a
same-origin Content Security Policy, disabled framing and MIME sniffing, and no external scripts,
styles, fonts, or analytics.

Keep the Bearer key only in JavaScript memory. It is supplied to same-origin API calls and discarded
on disconnect or page reload. Tenant-scoped users who cannot use platform discovery may enter their
assigned tenant id and provider id manually.

Expose three small discovery additions rather than create a parallel backend:

- platform-admin tenant listing;
- tenant-scoped DataSource listing;
- platform-admin runtime capabilities containing only service version, catalog backend, supported
  dialect identifiers, and configured provider identifiers.

The Control Plane registers DataSources for PostgreSQL, MySQL, MariaDB, Oracle, and SQL Server and
accepts only the existing opaque `connection_secret_ref`. Catalog acquisition reuses direct
introspection, non-executing DDL parsing, and validated manual snapshots. Schema Explorer renders the
latest catalog read model.

Query Studio invokes the existing lifecycle unchanged. It displays the structured proposal and AST
validation, permits `EXPLAIN`, then separately requires approval and read-only execution. Results are
rendered from the deterministic response together with privacy and provenance metadata. The client
does not decide whether SQL is safe or executable.

## Consequences

- One process and one origin are sufficient for local operation, testing, and initial deployments.
- UI and API remain version-aligned and the console is included in the Python wheel.
- A page reload requires re-authentication by design.
- Production vault/KMS integration, credential rotation, connection testing, semantic/FinOps/security
  administration, typed general-query parameters, saved workspaces, and live multi-database browser
  tests remain separate increments.
- The static client is intentionally thin. If interaction complexity later justifies a framework, it
  must preserve the same API, authentication, CSP, and governance boundaries.
