# Catalog backup and restore

`sqlverity-catalog-admin` implements checksummed backups for both supported catalog
backends. Run it with the same catalog and secret-manager environment variables as
the API. Backup files are accompanied by a `.manifest.json` file containing their
backend, size, SHA-256 checksum, creation time, and PostgreSQL database name where
applicable. The manifest never contains credentials.

## Backup and verification

```console
sqlverity-catalog-admin backup --output /backups/sqlverity-2026-08-22.dump
sqlverity-catalog-admin verify --backup /backups/sqlverity-2026-08-22.dump
```

SQLite uses the online backup API followed by `PRAGMA integrity_check`. PostgreSQL
uses `pg_dump --format=custom`, then validates the archive with `pg_restore --list`.
The PostgreSQL client tools must be installed in the operator image or host.
The standard SQLVerity AI image includes them; a Compose backup can therefore be written to its persistent
`/data` volume with `docker compose exec sqlverity sqlverity-catalog-admin backup --output
/data/backups/catalog.dump`.

## Restore drill

Use the drill command for routine evidence. SQLite is restored into a temporary directory.
PostgreSQL requires an explicit secret reference whose database name differs from the source
recorded in the manifest. The command verifies the checksum/archive, restores, opens the
catalog through the production repository, runs a health query, and counts tenants:

```console
sqlverity-catalog-admin drill --backup /backups/sqlverity-2026-08-22.sqlite3
sqlverity-catalog-admin drill \
  --backup /backups/sqlverity-2026-08-22.dump \
  --target-secret-ref env://SQLVERITY_DRILL_DB
```

The PostgreSQL drill target is cleaned by `pg_restore`; it must be an isolated, disposable
database. The command rejects a target with the source database name. The scheduled workflow
`.github/workflows/disaster-recovery-drill.yml` exercises this path weekly and on demand.

For an actual recovery, stop every SQLVerity AI API replica before restoring. Keep the pre-restore
database backup until application-level smoke tests have passed. Restoration is deliberately
gated by an exact target confirmation:

```console
# SQLite: confirmation is the exact configured SQLVERITY_CATALOG_PATH.
sqlverity-catalog-admin restore \
  --backup /backups/sqlverity-2026-08-22.sqlite3 \
  --confirm-target /data/sqlverity_catalog.sqlite3

# PostgreSQL: confirmation is the exact database name from the resolved secret.
sqlverity-catalog-admin restore \
  --backup /backups/sqlverity-2026-08-22.dump \
  --confirm-target sqlverity
```

After an actual restoration, restart one API replica and require `/health/ready` to succeed.
Verify a tenant, its latest catalog version, provider policies, security principals,
and an audit export before restoring normal traffic. Run this drill against a
non-production target on every release; retain the command output with the release
evidence.
