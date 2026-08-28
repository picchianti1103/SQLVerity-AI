from __future__ import annotations

import unittest

from packages.catalog.sqlverity_catalog.ingestion import (
    CatalogIngestionService,
    DataSourceNotFoundError,
)
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    RelationshipSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceType,
    EpistemicStatus,
    ObjectKind,
    SemanticDefinition,
)
from packages.retrieval.sqlverity_retrieval import ContextBuilderService, ContextNoMatchesError


class ContextBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        report = CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.data_source.id,
            DataSourceSnapshot(
                data_source_id=self.data_source.id,
                dialect="postgresql",
                objects=(
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="customers",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "bigint", 1, False, is_primary_key=True),
                            ColumnSnapshot(
                                "email",
                                "varchar(255)",
                                2,
                                False,
                                classification=Classification.PII,
                            ),
                        ),
                    ),
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="orders",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "bigint", 1, False, is_primary_key=True),
                            ColumnSnapshot("customer_id", "bigint", 2, False),
                            ColumnSnapshot(
                                "total_amount",
                                "numeric(18,2)",
                                3,
                                False,
                                classification=Classification.CONFIDENTIAL,
                            ),
                        ),
                    ),
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="products",
                        kind=ObjectKind.TABLE,
                        columns=(ColumnSnapshot("id", "bigint", 1, False),),
                    ),
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="audit_log",
                        kind=ObjectKind.TABLE,
                        columns=(ColumnSnapshot("id", "bigint", 1, False),),
                    ),
                ),
                relationships=(
                    RelationshipSnapshot(
                        name="orders_customer_fkey",
                        source_object_ref="public.orders",
                        target_object_ref="public.customers",
                        source_columns=("customer_id",),
                        target_columns=("id",),
                    ),
                ),
            ),
        )
        self.catalog_version_id = report.catalog_version_id
        self.builder = ContextBuilderService(self.repository)
        self._semantics(
            "public.orders",
            "Sales transaction records",
            EpistemicStatus.CONFIRMED,
        )
        self._semantics(
            "public.customers",
            "Registered buyers",
            EpistemicStatus.CONFIRMED,
        )
        self._semantics(
            "public.orders.total_amount",
            "Gross order amount",
            EpistemicStatus.CONFIRMED,
        )
        self._semantics(
            "public.products",
            "Inventory catalog",
            EpistemicStatus.IMPORTED,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_lexical_seed_expands_relationship_graph(self) -> None:
        context = self.builder.build(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="Show orders",
            max_seed_objects=1,
            max_objects=2,
            graph_hops=1,
            target_columns_per_object=2,
        )

        self.assertEqual(
            ("public.orders", "public.customers"),
            tuple(item.reference for item in context.objects),
        )
        self.assertFalse(context.objects[0].graph_expanded)
        self.assertTrue(context.objects[1].graph_expanded)
        self.assertIn(
            "relationship:orders_customer_fkey",
            context.objects[1].selection_reasons,
        )
        self.assertEqual(1, len(context.relationships))
        self.assertEqual("customer_id", context.relationships[0].source_columns[0])
        self.assertEqual(2, context.omitted_object_count)

    def test_confirmed_column_description_drives_retrieval(self) -> None:
        context = self.builder.build(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="gross amount",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
            target_columns_per_object=1,
        )

        self.assertEqual("public.orders", context.objects[0].reference)
        self.assertIn(
            "confirmed_column_description:total_amount",
            context.objects[0].selection_reasons,
        )
        column_names = {column.name for column in context.objects[0].columns}
        self.assertIn("total_amount", column_names)
        self.assertIn("id", column_names)

    def test_catalog_columns_and_semantics_are_loaded_in_bulk(self) -> None:
        statements: list[str] = []
        self.repository._connection.set_trace_callback(statements.append)  # noqa: SLF001
        try:
            self.builder.build(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query="gross orders",
            )
        finally:
            self.repository._connection.set_trace_callback(None)  # noqa: SLF001

        selects = tuple(statement.casefold() for statement in statements)
        self.assertEqual(
            1,
            sum("from column_definitions" in statement for statement in selects),
        )
        self.assertEqual(
            1,
            sum("from semantic_resolutions" in statement for statement in selects),
        )

    def test_imported_description_is_not_used_as_governed_retrieval_text(self) -> None:
        with self.assertRaises(ContextNoMatchesError):
            self.builder.build(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query="inventory catalog",
                graph_hops=0,
            )

    def test_graph_expansion_can_be_disabled(self) -> None:
        context = self.builder.build(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="orders",
            max_seed_objects=1,
            max_objects=2,
            graph_hops=0,
        )

        self.assertEqual(("public.orders",), tuple(item.reference for item in context.objects))
        self.assertEqual((), context.relationships)

    def test_unmatched_query_fails_closed(self) -> None:
        with self.assertRaises(ContextNoMatchesError):
            self.builder.build(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query="meteorological observations",
            )

    def test_context_lookup_cannot_cross_tenant_boundary(self) -> None:
        other_tenant = self.repository.create_tenant("Other")

        with self.assertRaises(DataSourceNotFoundError):
            self.builder.build(
                tenant_id=other_tenant.id,
                data_source_id=self.data_source.id,
                query="orders",
            )

    def _semantics(
        self,
        object_ref: str,
        description: str,
        status: EpistemicStatus,
    ) -> None:
        self.repository.propose_semantic_definition(
            SemanticDefinition(
                tenant_id=self.tenant.id,
                catalog_version_id=self.catalog_version_id,
                object_ref=object_ref,
                description=description,
                status=status,
                source=f"test:{status.value}",
                confidence=1.0,
            )
        )


if __name__ == "__main__":
    unittest.main()
