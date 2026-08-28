from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from packages.catalog.sqlverity_catalog.repository import (
    _migration_body,
    _normalize_postgresql_row,
    _postgresql_migration_files,
    _translate_postgresql_query,
)


class PostgreSQLCatalogRepositoryAdapterTests(unittest.TestCase):
    def test_sqlite_placeholders_json_columns_and_collation_are_translated(self) -> None:
        translated = _translate_postgresql_query(
            """
            SELECT capabilities_json, details_json FROM data_sources
            WHERE tenant_id = ?
            ORDER BY name COLLATE NOCASE, id
            """
        )

        self.assertIn("capabilities", translated)
        self.assertIn("details", translated)
        self.assertNotIn("_json", translated)
        self.assertIn("tenant_id = %s", translated)
        self.assertIn("ORDER BY lower(name), id", translated)

    def test_postgresql_rows_are_normalized_for_shared_domain_mappers(self) -> None:
        identifier = uuid4()
        created_at = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)

        row = _normalize_postgresql_row(
            {
                "id": identifier,
                "created_at": created_at,
                "capabilities": ["introspect", "execute_read_only"],
                "details": {"event": "created"},
            }
        )

        self.assertEqual(str(identifier), row["id"])
        self.assertEqual(created_at.isoformat(), row["created_at"])
        self.assertEqual(
            ["introspect", "execute_read_only"],
            json.loads(row["capabilities_json"]),
        )
        self.assertEqual({"event": "created"}, json.loads(row["details_json"]))

    def test_all_packaged_migrations_are_discovered_and_outer_transaction_removed(self) -> None:
        migrations = _postgresql_migration_files()

        self.assertEqual(16, len(migrations))
        self.assertEqual("0001_catalog.sql", migrations[0].name)
        self.assertEqual("0016_privacy_first_ai_egress.sql", migrations[-1].name)
        body = _migration_body("BEGIN;\nSELECT 1;\nCOMMIT;\n")
        self.assertEqual("\nSELECT 1;\n", body)


if __name__ == "__main__":
    unittest.main()
