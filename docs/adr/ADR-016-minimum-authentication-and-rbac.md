# ADR-016: Minimum authentication, scoped RBAC, and server-derived actors

- **Status:** Accepted
- **Date:** 2026-08-16

## Context

The structural document makes the tenant the primary security boundary, calls for RBAC across
administrator, data steward, analyst, and viewer roles, and requires DataSource authorization. The
previous API isolated catalog reads by tenant id but trusted any caller that knew an id. Human
correction, feedback, golden-review, and query-approval payloads also supplied their own `actor_id`,
so audit attribution was not an identity assertion.

Full SSO and advanced RBAC belong to Enterprise Phase 10. The MVP still needs a fail-closed identity
boundary that can be exercised locally, deployed without storing plaintext secrets, and later
replaced by a federated identity adapter.

## Decision

Every `/v1` request requires an HTTP Bearer credential. `/health` remains public for liveness.
OpenAPI declares the Bearer scheme globally and explicitly exempts the health operation.

The first platform administrator authenticates with `SQLVERITY_BOOTSTRAP_API_KEY`, which must contain at
least 32 characters. Its SHA-256 digest lives only in process memory; the key and digest are not
written to the catalog or audit log. Missing configuration prevents application startup.

Administrators provision tenant-scoped principals through the security API. Provisioning creates:

- an immutable principal identified by tenant, subject, and display name;
- one opaque, high-entropy API key returned only in the creation response;
- an immutable credential containing only the SHA-256 token digest, label, timestamps, and optional
  expiry;
- either one tenant role assignment or the same role across explicit DataSource assignments.

Credential revocation is a separate append-only record. Authentication rejects unknown, expired,
or revoked credentials with the same generic response. Administrative listings expose credential
ids, labels, expiry, creation time, and revocation state, never token material or token hashes.

The roles and effective permissions are:

| Role | Effective MVP authority |
|---|---|
| `admin` | Security, DataSource, semantic, query, feedback, approval, golden, FinOps, audit/read |
| `data_steward` | DataSource/schema, semantic, query, feedback, approval, golden, audit/read |
| `analyst` | Query/context, feedback/corrections, approval/execution, read |
| `viewer` | Read only |

Tenant assignments apply to every DataSource in that tenant. DataSource assignments apply only to
the named sources and do not grant tenant-wide listing or access to sibling sources. The bootstrap
principal is the only `platform.manage` authority.

All `/v1/tenants/{tenant_id}` requests first enforce tenant access. Nested DataSource requests enforce
the exact DataSource scope before reaching the endpoint. Sensitive mutations then require their
specific permission. Pydantic forbids the former `actor_id` input on human-authored evidence and
approval contracts; services receive the authenticated principal id instead.

SQLite implements the development/test schema and immutable triggers. PostgreSQL migration `0010`
provides matching composite tenant foreign keys, constraints, indexes, and immutable triggers.

## Consequences

- Knowledge of tenant or DataSource UUIDs no longer grants API access.
- Actor attribution for semantic evidence, corrected SQL, feedback, golden review, credential
  revocation, and query approval is derived from authenticated state.
- API keys cannot be recovered from the catalog; losing a returned key requires revocation and a
  newly provisioned principal in this MVP lifecycle.
- Revocation is immediate and retains immutable audit evidence without persisting the reason in
  content-minimizing audit details.
- Bootstrap-key custody is operationally critical and must move to a deployment secret manager.
- SHA-256 is appropriate for generated high-entropy opaque keys; this decision does not permit
  password authentication or low-entropy shared secrets.
- SSO/OIDC/SAML, MFA, group synchronization, role mutation/removal, automated credential rotation,
  break-glass controls, and row/column authorization remain explicit Enterprise work.
