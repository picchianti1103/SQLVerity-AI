# Operational retention

SQLVerity AI keeps audit events, semantic evidence, security assignments, usage events, feedback,
and review decisions immutable. The operational retention command never deletes those records.
It only removes terminal background jobs and inactive request-quota windows older than an
explicit cutoff. Queued/running jobs and active quota leases are always preserved.

Preview first, then apply using the same timezone-aware ISO-8601 cutoff twice:

```console
sqlverity-retention preview --before 2026-05-01T00:00:00Z
sqlverity-retention apply \
  --before 2026-05-01T00:00:00Z \
  --confirm-before 2026-05-01T00:00:00Z \
  --actor-id operations-retention
```

The result is JSON. Every applied run is recorded in the immutable
`operational_retention_runs` table with the cutoff, actor, completion time, and aggregate delete
counts. Schedule this command in the deployment orchestrator only after a successful backup.
Alert on a non-zero exit code and retain the JSON output with operations evidence.

Audit export retention belongs in the immutable destination (for example, object storage with
retention lock), not in the live SQLVerity AI catalog. Any future policy that removes immutable
evidence requires a separate legal/security design and a new migration; it must not reuse this
operational command.
