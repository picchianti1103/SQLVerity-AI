from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packages.catalog.sqlverity_catalog.backup import (
    CatalogBackupError,
    create_sqlite_backup,
    drill_sqlite_backup,
    restore_sqlite_backup,
    verify_backup,
)
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository


class CatalogBackupTests(unittest.TestCase):
    def test_sqlite_backup_is_verified_and_can_be_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "catalog.sqlite3"
            backup_path = root / "backup.sqlite3"
            repository = SQLiteCatalogRepository(catalog_path)
            repository.initialize()
            tenant = repository.create_tenant("Acme")
            repository.close()

            manifest = create_sqlite_backup(catalog_path, backup_path)

            self.assertEqual("sqlite", manifest.backend)
            self.assertEqual(manifest, verify_backup(backup_path))
            modified_repository = SQLiteCatalogRepository(catalog_path)
            modified_repository.create_tenant("Temporary tenant")
            modified_repository.close()
            restore_sqlite_backup(
                backup_path,
                catalog_path,
                confirmed_destination=catalog_path,
            )
            restored_repository = SQLiteCatalogRepository(catalog_path)
            restored_tenant_ids = tuple(
                item.id for item in restored_repository.list_tenants()
            )
            self.assertEqual((tenant.id,), restored_tenant_ids)
            restored_repository.close()

    def test_restore_requires_an_exact_target_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "catalog.sqlite3"
            backup_path = root / "backup.sqlite3"
            repository = SQLiteCatalogRepository(catalog_path)
            repository.initialize()
            repository.close()
            create_sqlite_backup(catalog_path, backup_path)

            with self.assertRaises(CatalogBackupError):
                restore_sqlite_backup(
                    backup_path,
                    catalog_path,
                    confirmed_destination=root / "wrong.sqlite3",
                )

    def test_checksum_detects_a_tampered_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "catalog.sqlite3"
            backup_path = root / "backup.sqlite3"
            repository = SQLiteCatalogRepository(catalog_path)
            repository.initialize()
            repository.close()
            create_sqlite_backup(catalog_path, backup_path)
            with backup_path.open("ab") as stream:
                stream.write(b"tampered")

            with self.assertRaisesRegex(CatalogBackupError, "checksum"):
                verify_backup(backup_path)

    def test_restore_drill_uses_an_isolated_sqlite_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "catalog.sqlite3"
            backup_path = root / "backup.sqlite3"
            repository = SQLiteCatalogRepository(catalog_path)
            repository.initialize()
            repository.create_tenant("Acme")
            repository.close()
            create_sqlite_backup(catalog_path, backup_path)

            report = drill_sqlite_backup(backup_path)

            self.assertEqual("sqlite", report.backend)
            self.assertEqual(1, report.tenant_count)
            self.assertTrue(catalog_path.exists())


if __name__ == "__main__":
    unittest.main()
