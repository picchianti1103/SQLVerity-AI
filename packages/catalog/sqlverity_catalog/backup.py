from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from packages.connectors.sqlverity_connectors.connection import (
    DatabaseSecretResolver,
    PostgreSQLConnectionSecret,
    load_secret_resolver_from_environment,
)


class CatalogBackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backend: str
    created_at: str
    sha256: str
    size_bytes: int
    database: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreDrillReport:
    backend: str
    tenant_count: int
    verified_at: str
    target_database: str | None = None


def create_sqlite_backup(source: Path, destination: Path) -> BackupManifest:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise CatalogBackupError("SQLite catalog file does not exist")
    _require_new_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(source)) as source_connection:
            with closing(sqlite3.connect(destination)) as destination_connection:
                source_connection.backup(destination_connection)
        _verify_sqlite_integrity(destination)
        return _write_manifest(destination, backend="sqlite")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def restore_sqlite_backup(
    backup: Path,
    destination: Path,
    *,
    confirmed_destination: Path,
) -> None:
    backup = backup.resolve()
    destination = destination.resolve()
    if confirmed_destination.resolve() != destination:
        raise CatalogBackupError("Restore confirmation does not match the catalog path")
    verify_backup(backup, expected_backend="sqlite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".restore",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(backup, temporary_path)
        _verify_sqlite_integrity(temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def create_postgresql_backup(
    secret: PostgreSQLConnectionSecret,
    destination: Path,
    *,
    executable: str = "pg_dump",
) -> BackupManifest:
    destination = destination.resolve()
    _require_new_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(destination),
        "--host",
        secret.host,
        "--port",
        str(secret.port),
        "--username",
        secret.username,
        "--dbname",
        secret.database,
    ]
    try:
        _run_postgresql_tool(command, secret)
        verify_postgresql_archive(destination)
        return _write_manifest(
            destination,
            backend="postgresql",
            database=secret.database,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def restore_postgresql_backup(
    secret: PostgreSQLConnectionSecret,
    backup: Path,
    *,
    confirmed_database: str,
    executable: str = "pg_restore",
    allow_source_database_mismatch: bool = False,
) -> None:
    if confirmed_database != secret.database:
        raise CatalogBackupError("Restore confirmation does not match the database name")
    backup = backup.resolve()
    manifest = verify_backup(backup, expected_backend="postgresql")
    if (
        not allow_source_database_mismatch
        and manifest.database is not None
        and manifest.database != secret.database
    ):
        raise CatalogBackupError("Backup belongs to another database")
    verify_postgresql_archive(backup, executable=executable)
    _run_postgresql_tool(
        [
            executable,
            "--exit-on-error",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--host",
            secret.host,
            "--port",
            str(secret.port),
            "--username",
            secret.username,
            "--dbname",
            secret.database,
            str(backup),
        ],
        secret,
    )


def drill_sqlite_backup(backup: Path) -> RestoreDrillReport:
    verify_backup(backup, expected_backend="sqlite")
    from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository

    with tempfile.TemporaryDirectory(prefix="sqlverity-restore-drill-") as directory:
        target = Path(directory) / "restored.sqlite3"
        restore_sqlite_backup(backup, target, confirmed_destination=target)
        repository = SQLiteCatalogRepository(target)
        try:
            if not repository.health_check():
                raise CatalogBackupError("Restored SQLite catalog health check failed")
            tenant_count = len(repository.list_tenants())
        finally:
            repository.close()
    return RestoreDrillReport(
        backend="sqlite",
        tenant_count=tenant_count,
        verified_at=datetime.now(UTC).isoformat(),
    )


def drill_postgresql_backup(
    backup: Path,
    target: PostgreSQLConnectionSecret,
    *,
    executable: str = "pg_restore",
) -> RestoreDrillReport:
    manifest = verify_backup(backup, expected_backend="postgresql")
    if manifest.database is None:
        raise CatalogBackupError("PostgreSQL backup manifest has no source database")
    if target.database == manifest.database:
        raise CatalogBackupError("Restore drill target must use a different database name")
    restore_postgresql_backup(
        target,
        backup,
        confirmed_database=target.database,
        executable=executable,
        allow_source_database_mismatch=True,
    )
    from packages.catalog.sqlverity_catalog.repository import PostgreSQLCatalogRepository

    repository = PostgreSQLCatalogRepository(
        target.as_connect_kwargs(application_name="sqlverity-restore-drill"),
        min_pool_size=1,
        max_pool_size=2,
    )
    try:
        if not repository.health_check():
            raise CatalogBackupError("Restored PostgreSQL catalog health check failed")
        tenant_count = len(repository.list_tenants())
    finally:
        repository.close()
    return RestoreDrillReport(
        backend="postgresql",
        tenant_count=tenant_count,
        verified_at=datetime.now(UTC).isoformat(),
        target_database=target.database,
    )


def verify_backup(backup: Path, *, expected_backend: str | None = None) -> BackupManifest:
    backup = backup.resolve()
    if not backup.is_file():
        raise CatalogBackupError("Backup file does not exist")
    manifest_path = _manifest_path(backup)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = BackupManifest(**payload)
    except (OSError, TypeError, ValueError) as error:
        raise CatalogBackupError("Backup manifest is missing or invalid") from error
    if expected_backend is not None and manifest.backend != expected_backend:
        raise CatalogBackupError("Backup backend does not match the requested restore backend")
    if backup.stat().st_size != manifest.size_bytes or _sha256(backup) != manifest.sha256:
        raise CatalogBackupError("Backup checksum verification failed")
    if manifest.backend == "sqlite":
        _verify_sqlite_integrity(backup)
    elif manifest.backend != "postgresql":
        raise CatalogBackupError("Backup manifest has an unsupported backend")
    return manifest


def verify_postgresql_archive(
    backup: Path,
    *,
    executable: str = "pg_restore",
) -> None:
    try:
        subprocess.run(
            [executable, "--list", str(backup.resolve())],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CatalogBackupError("PostgreSQL backup archive verification failed") from error


def _run_postgresql_tool(
    command: Sequence[str],
    secret: PostgreSQLConnectionSecret,
) -> None:
    environment = dict(os.environ)
    environment.update({"PGPASSWORD": secret.password, "PGSSLMODE": secret.sslmode})
    try:
        subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CatalogBackupError("PostgreSQL backup command failed") from error


def _write_manifest(
    backup: Path,
    *,
    backend: str,
    database: str | None = None,
) -> BackupManifest:
    manifest = BackupManifest(
        backend=backend,
        created_at=datetime.now(UTC).isoformat(),
        sha256=_sha256(backup),
        size_bytes=backup.stat().st_size,
        database=database,
    )
    _manifest_path(backup).write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _verify_sqlite_integrity(path: Path) -> None:
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise CatalogBackupError("SQLite backup cannot be opened") from error
    if result is None or result[0] != "ok":
        raise CatalogBackupError("SQLite backup integrity check failed")


def _require_new_destination(destination: Path) -> None:
    if destination.exists() or _manifest_path(destination).exists():
        raise CatalogBackupError("Backup destination already exists")


def _manifest_path(backup: Path) -> Path:
    return backup.with_name(f"{backup.name}.manifest.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _postgresql_secret(resolver: DatabaseSecretResolver) -> PostgreSQLConnectionSecret:
    secret_ref = os.environ.get("SQLVERITY_CATALOG_SECRET_REF", "").strip()
    if not secret_ref:
        raise CatalogBackupError("SQLVERITY_CATALOG_SECRET_REF is required for PostgreSQL")
    return resolver.resolve_postgresql(secret_ref)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up, verify, or restore the SQLVerity AI catalog"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument(
        "--confirm-target",
        required=True,
        help="Exact SQLite catalog path or PostgreSQL database name",
    )
    drill = subparsers.add_parser("drill")
    drill.add_argument("--backup", type=Path, required=True)
    drill.add_argument(
        "--target-secret-ref",
        help="Required for PostgreSQL; must resolve to an isolated target database",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "drill":
        drill_manifest = verify_backup(args.backup)
        if drill_manifest.backend == "sqlite":
            drill_report = drill_sqlite_backup(args.backup)
        else:
            target_secret_ref = (args.target_secret_ref or "").strip()
            if not target_secret_ref:
                raise CatalogBackupError(
                    "--target-secret-ref is required for a PostgreSQL restore drill"
                )
            target = load_secret_resolver_from_environment().resolve_postgresql(
                target_secret_ref
            )
            drill_report = drill_postgresql_backup(args.backup, target)
        print(json.dumps(asdict(drill_report), sort_keys=True))
        return 0
    backend = os.environ.get("SQLVERITY_CATALOG_BACKEND", "sqlite").strip().casefold()
    if args.command == "verify":
        manifest = verify_backup(args.backup)
    elif backend == "sqlite":
        catalog_path = Path(os.environ.get("SQLVERITY_CATALOG_PATH", "sqlverity_catalog.sqlite3"))
        if args.command == "backup":
            manifest = create_sqlite_backup(catalog_path, args.output)
        else:
            restore_sqlite_backup(
                args.backup,
                catalog_path,
                confirmed_destination=Path(args.confirm_target),
            )
            print("Catalog restore completed; restart the API before serving traffic.")
            return 0
    elif backend in {"postgres", "postgresql"}:
        secret = _postgresql_secret(load_secret_resolver_from_environment())
        if args.command == "backup":
            manifest = create_postgresql_backup(secret, args.output)
        else:
            restore_postgresql_backup(
                secret,
                args.backup,
                confirmed_database=args.confirm_target,
            )
            print("Catalog restore completed; restart the API before serving traffic.")
            return 0
    else:
        raise CatalogBackupError(f"Unsupported catalog backend: {backend}")
    print(json.dumps(asdict(manifest), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
