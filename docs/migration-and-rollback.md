# Catalog migration and rollback

SQLVerity AI applies packaged PostgreSQL catalog migrations during repository startup. A transaction-scoped
advisory lock serializes migration runners across replicas, and every applied filename is recorded in
`sqlverity_schema_migrations`. SQLite remains the local single-process adapter and upgrades its evolving
pre-release schema at startup.

Migrations are forward-only. SQLVerity AI does not pretend that destructive down migrations are safe:
application rollback after a catalog change uses a verified pre-upgrade backup and the application
revision that matches it.

## Pre-upgrade gate

1. Stop schema-changing administrative work and record the application commit and image digest.
2. Run `sqlverity-catalog-admin backup` and `verify` using the same catalog/secret configuration as the
   API.
3. Run `sqlverity-catalog-admin drill` against an isolated target and retain its checksum and output.
4. Review every not-yet-applied file in `migrations/postgresql` and confirm its lock/runtime impact
   against a production-shaped copy.
5. Confirm the old application image, backup, credentials, and rollback target remain available.

See [backup-and-restore.md](backup-and-restore.md) for exact commands. Never deploy a catalog migration
without a restorable pre-upgrade backup.

## Forward deployment

Drain traffic, then start one API replica on the new revision. Its repository initialization obtains
the migration lock and applies pending scripts transactionally. Require `/health/ready` to pass and
inspect the applied versions before restoring normal traffic:

```sql
SELECT version, applied_at
FROM sqlverity_schema_migrations
ORDER BY version;
```

Run a tenant read, Schema Explorer read, provider-policy read, principal read, background-job read,
AI transfer receipt read, and audit export. For migration `0016`, also issue a preflight and prove
that one replica can consume its confirmation while a second replica rejects replay through the
shared catalog. Only then roll out the remaining replicas. A failed migration prevents readiness;
do not bypass it by inserting or deleting migration-ledger rows.

## Application rollback

If the old application was explicitly verified as compatible with the migrated schema, drain the new
revision and redeploy the old image. Otherwise:

1. stop every API and worker replica;
2. preserve an additional backup of the failed upgraded state for diagnosis;
3. restore the verified pre-upgrade backup, preferably into a new isolated database;
4. point `SQLVERITY_CATALOG_SECRET_REF` at that database and deploy the matching old application image;
5. require readiness and repeat the post-restore checks before reopening traffic.

An in-place restore is the last resort and requires the exact target confirmation documented by the
backup tool. Never hand-edit catalog tables or remove an entry from `sqlverity_schema_migrations` to make
an older binary start.

## Existing Compose volumes

PostgreSQL runs `/docker-entrypoint-initdb.d` only when it creates an empty data volume. Reusing a
volume created before the isolated demo database was introduced therefore requires a one-time,
non-destructive initialization after setting `SQLVERITY_DEMO_DB_PASSWORD` in `.env`:

```console
docker compose up -d catalog-db
docker compose exec catalog-db sh /docker-entrypoint-initdb.d/00-create-demo.sh
docker compose exec catalog-db sh -c \
  'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --file /docker-entrypoint-initdb.d/10-demo.sql'
docker compose up -d --build sqlverity
```

The role/database creation and fixture inserts are repeatable; rerunning the role step also aligns the
demo password with `.env`. Do not use `docker compose down -v` on a populated catalog as an upgrade
mechanism because it deletes the PostgreSQL volume.

## Evidence to retain

Keep the source and target revisions, migration list, backup manifest and checksum, restore-drill
output, readiness result, smoke-test result, deployment timestamps, operator identity, and incident or
change record. Production RPO/RTO is a measured property of this procedure on the target platform,
not a guarantee made by the repository.
