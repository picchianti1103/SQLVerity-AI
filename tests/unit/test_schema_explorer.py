from __future__ import annotations

import unittest

from packages.catalog.sqlverity_catalog.explorer import (
    CatalogNotIngestedError,
    SchemaExplorerService,
)
from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.offline_import import (
    OfflineImportModeError,
    OfflineSchemaImportService,
)
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import Classification, DataSourceType, ObjectKind


class SchemaExplorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Manual catalog",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        ingestion = CatalogIngestionService(self.repository, {})
        self.importer = OfflineSchemaImportService(self.repository, ingestion)
        self.explorer = SchemaExplorerService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()

    def test_exposes_latest_version_columns_classification_and_semantics(self) -> None:
        snapshot = DataSourceSnapshot(
            data_source_id=self.data_source.id,
            dialect="postgresql",
            objects=(
                SchemaObjectSnapshot(
                    schema_name="public",
                    name="customers",
                    kind=ObjectKind.TABLE,
                    comment="Registered customers",
                    columns=(
                        ColumnSnapshot(
                            name="id",
                            physical_type="bigint",
                            ordinal=1,
                            nullable=False,
                            is_primary_key=True,
                        ),
                        ColumnSnapshot(
                            name="email",
                            physical_type="varchar(255)",
                            ordinal=2,
                            nullable=False,
                            comment="Customer email address",
                            classification=Classification.PII,
                        ),
                    ),
                ),
            ),
        )
        self.importer.import_manual(self.tenant.id, self.data_source.id, snapshot)

        explorer = self.explorer.get_latest(self.tenant.id, self.data_source.id)

        self.assertEqual(1, explorer.catalog_version)
        assert explorer.objects[0].semantics is not None
        self.assertEqual("Registered customers", explorer.objects[0].semantics.description)
        self.assertTrue(explorer.objects[0].columns[0].is_primary_key)
        self.assertEqual(Classification.PII, explorer.objects[0].columns[1].classification)
        assert explorer.objects[0].columns[1].semantics is not None
        self.assertEqual(
            "Customer email address",
            explorer.objects[0].columns[1].semantics.description,
        )

    def test_reports_when_no_catalog_version_exists(self) -> None:
        with self.assertRaises(CatalogNotIngestedError):
            self.explorer.get_latest(self.tenant.id, self.data_source.id)

    def test_manual_import_requires_a_compatible_data_source_type(self) -> None:
        direct_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Direct",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
        )
        snapshot = DataSourceSnapshot(
            data_source_id=direct_source.id,
            dialect="postgresql",
            objects=(
                SchemaObjectSnapshot(
                    schema_name="public",
                    name="events",
                    kind=ObjectKind.TABLE,
                    columns=(ColumnSnapshot("id", "uuid", 1, False),),
                ),
            ),
        )

        with self.assertRaises(OfflineImportModeError):
            self.importer.import_manual(self.tenant.id, direct_source.id, snapshot)


if __name__ == "__main__":
    unittest.main()
