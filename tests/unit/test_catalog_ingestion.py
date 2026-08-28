from __future__ import annotations

import unittest

from packages.catalog.sqlverity_catalog.ingestion import (
    CatalogIngestionError,
    CatalogIngestionService,
)
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.connectors.sqlverity_connectors.postgresql import PostgreSQLConnector
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    RelationshipSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
    EpistemicStatus,
    ObjectKind,
)
from tests.unit.test_postgresql_connector import FakeConnectFactory, FakeSecretResolver


class CatalogIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
            capabilities={DataSourceCapability.INTROSPECT},
            connection_secret_ref="vault://acme/analytics",
        )
        connector = PostgreSQLConnector(FakeSecretResolver(), FakeConnectFactory())
        self.service = CatalogIngestionService(
            self.repository,
            {"postgresql": connector},
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_ingests_a_versioned_schema_graph_and_imported_semantics(self) -> None:
        report = self.service.ingest(self.tenant.id, self.data_source.id)

        self.assertEqual(1, report.catalog_version)
        self.assertEqual(3, report.object_count)
        self.assertEqual(6, report.column_count)
        self.assertEqual(1, report.relationship_count)
        self.assertEqual(5, report.imported_description_count)

        objects = self.repository.list_schema_objects(
            self.tenant.id,
            report.catalog_version_id,
        )
        orders = next(item for item in objects if item.reference == "public.orders")
        columns = self.repository.list_columns(self.tenant.id, orders.id)
        relationships = self.repository.list_relationships(
            self.tenant.id,
            report.catalog_version_id,
        )
        amount_semantics = self.repository.get_semantic_resolution(
            self.tenant.id,
            self.data_source.id,
            "public.orders.total_amount",
        )

        assert amount_semantics is not None
        self.assertEqual("id", columns[0].name)
        self.assertTrue(columns[0].is_primary_key)
        self.assertEqual("0", columns[2].default_expression)
        self.assertEqual(("customer_id",), relationships[0].source_columns)
        self.assertEqual(EpistemicStatus.IMPORTED, amount_semantics.status)
        self.assertEqual("Gross order amount", amount_semantics.description)

    def test_reingestion_creates_a_new_catalog_version(self) -> None:
        first = self.service.ingest(self.tenant.id, self.data_source.id)
        second = self.service.ingest(self.tenant.id, self.data_source.id)

        self.assertEqual(1, first.catalog_version)
        self.assertEqual(2, second.catalog_version)
        self.assertNotEqual(first.catalog_version_id, second.catalog_version_id)

    def test_connection_test_does_not_create_a_catalog_version_and_is_audited(self) -> None:
        report = self.service.test_connection(self.tenant.id, self.data_source.id)

        self.assertEqual(self.data_source.id, report.data_source_id)
        self.assertEqual(3, report.object_count)
        self.assertIn("introspect", report.capabilities)
        self.assertIsNone(
            self.repository.get_latest_catalog_version(self.tenant.id, self.data_source.id)
        )
        matching_events = [
            event
            for event in self.repository.audit_events(self.tenant.id)
            if event.event_type == "data_source.connection_test_succeeded"
        ]
        self.assertEqual(1, len(matching_events))
        self.assertNotIn("connection_secret_ref", matching_events[0].details)

    def test_invalid_relationship_is_rejected_before_creating_a_version(self) -> None:
        class InvalidConnector:
            def capabilities(
                self,
                data_source: DataSource,
            ) -> frozenset[DataSourceCapability]:
                return frozenset({DataSourceCapability.INTROSPECT})

            def introspect(self, data_source: DataSource) -> DataSourceSnapshot:
                return DataSourceSnapshot(
                    data_source_id=data_source.id,
                    dialect="postgresql",
                    objects=(
                        SchemaObjectSnapshot(
                            schema_name="public",
                            name="orders",
                            kind=ObjectKind.TABLE,
                            columns=(ColumnSnapshot("id", "bigint", 1, False),),
                        ),
                    ),
                    relationships=(
                        RelationshipSnapshot(
                            name="orders_customer_fkey",
                            source_object_ref="public.orders",
                            target_object_ref="public.missing_customers",
                            source_columns=("id",),
                            target_columns=("id",),
                        ),
                    ),
                )

        service = CatalogIngestionService(
            self.repository,
            {"postgresql": InvalidConnector()},
        )

        with self.assertRaises(CatalogIngestionError):
            service.ingest(self.tenant.id, self.data_source.id)

        first_version = self.repository.create_catalog_version(
            self.tenant.id,
            self.data_source.id,
        )
        self.assertEqual(1, first_version.version)


if __name__ == "__main__":
    unittest.main()
