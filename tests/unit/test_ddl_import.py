from __future__ import annotations

import unittest

from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.connectors.sqlverity_connectors.ddl import DDLParseError, PostgreSQLDDLParser
from packages.domain.sqlverity_domain.models import DataSourceType, EpistemicStatus, ObjectKind

DDL = """
CREATE SCHEMA reporting;

CREATE TABLE public.customers (
    id BIGSERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL
);

CREATE TABLE public.orders (
    id BIGINT,
    customer_id BIGINT NOT NULL,
    total_amount NUMERIC(18, 2) DEFAULT 0,
    CONSTRAINT orders_pkey PRIMARY KEY (id),
    CONSTRAINT orders_customer_fkey
        FOREIGN KEY (customer_id) REFERENCES public.customers (id)
);

CREATE VIEW reporting.order_totals AS
SELECT customer_id, SUM(total_amount) AS total
FROM public.orders
GROUP BY customer_id;

COMMENT ON TABLE public.orders IS 'Sales orders';
COMMENT ON COLUMN public.orders.total_amount IS 'Gross order amount';
"""


class PostgreSQLDDLParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = PostgreSQLDDLParser()

    def test_parses_tables_views_keys_defaults_relationships_and_comments(self) -> None:
        snapshot = self.parser.parse(data_source_id="source-1", ddl=DDL)

        self.assertEqual("postgresql", snapshot.dialect)
        self.assertEqual(3, len(snapshot.objects))
        orders = next(item for item in snapshot.objects if item.reference == "public.orders")
        view = next(item for item in snapshot.objects if item.reference == "reporting.order_totals")
        self.assertEqual(ObjectKind.TABLE, orders.kind)
        self.assertEqual("Sales orders", orders.comment)
        self.assertTrue(orders.columns[0].is_primary_key)
        self.assertFalse(orders.columns[0].nullable)
        self.assertEqual("0", orders.columns[2].default_expression)
        self.assertEqual("Gross order amount", orders.columns[2].comment)
        self.assertEqual(ObjectKind.VIEW, view.kind)
        self.assertEqual(("customer_id", "total"), tuple(column.name for column in view.columns))
        assert view.definition_sql is not None
        self.assertIn("SUM(total_amount)", view.definition_sql)
        self.assertEqual(1, len(snapshot.relationships))
        self.assertEqual("public.customers", snapshot.relationships[0].target_object_ref)

    def test_rejects_non_catalog_ddl(self) -> None:
        with self.assertRaises(DDLParseError):
            self.parser.parse(data_source_id="source-1", ddl="DROP TABLE public.orders")

    def test_uses_configured_default_schema(self) -> None:
        snapshot = self.parser.parse(
            data_source_id="source-1",
            ddl="CREATE TABLE events (id UUID PRIMARY KEY)",
            default_schema="analytics",
        )

        self.assertEqual("analytics.events", snapshot.objects[0].reference)


class DDLIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Imported DDL",
            source_type=DataSourceType.DDL_IMPORT,
            dialect="postgresql",
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_persists_a_parsed_snapshot_as_a_catalog_version(self) -> None:
        snapshot = PostgreSQLDDLParser().parse(
            data_source_id=self.data_source.id,
            ddl=DDL,
        )
        report = CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.data_source.id,
            snapshot,
        )

        self.assertEqual(1, report.catalog_version)
        self.assertEqual(3, report.object_count)
        semantics = self.repository.get_semantic_resolution(
            self.tenant.id,
            self.data_source.id,
            "public.orders.total_amount",
        )
        assert semantics is not None
        self.assertEqual(EpistemicStatus.IMPORTED, semantics.status)


if __name__ == "__main__":
    unittest.main()
