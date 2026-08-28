from __future__ import annotations

import sqlite3
import unittest

from packages.catalog.sqlverity_catalog.ingestion import (
    CatalogIngestionService,
    DataSourceNotFoundError,
)
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceType,
    ObjectKind,
)
from packages.learning.sqlverity_learning import (
    CorrectedSQLConcurrencyError,
    CorrectedSQLExampleEntry,
    CorrectedSQLValidationError,
    LearningLoopService,
)
from packages.retrieval.sqlverity_retrieval import ContextBuilderService
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator


class LearningLoopTests(unittest.TestCase):
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
        self._ingest_orders_schema()
        self.service = LearningLoopService(
            self.repository,
            PostgreSQLSQLValidator(),
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_records_validated_immutable_evidence_without_audit_content(self) -> None:
        entry = self._correct()

        self.assertTrue(entry.is_active)
        self.assertEqual(entry.example.revision, 1)
        self.assertEqual(
            entry.example.normalized_sql,
            "SELECT id, total_amount FROM public.orders LIMIT 500",
        )
        self.assertEqual(entry.example.referenced_tables, ("public.orders",))
        self.assertEqual(
            entry.example.referenced_columns,
            ("public.orders.id", "public.orders.total_amount"),
        )
        audit = self.repository.audit_events(self.tenant.id)[-1]
        self.assertEqual(audit.event_type, "learning.corrected_sql_recorded")
        self.assertNotIn("question", audit.details)
        self.assertNotIn("sql", audit.details)

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(  # noqa: SLF001
                "UPDATE corrected_sql_examples SET actor_id = ? WHERE id = ?",
                ("tampered", entry.example.id),
            )

    def test_unsafe_correction_fails_before_persistence(self) -> None:
        with self.assertRaises(CorrectedSQLValidationError) as raised:
            self.service.correct(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                question="Cancella gli ordini",
                corrected_sql="DELETE FROM public.orders",
                actor_id="steward-1",
                content_classification=Classification.INTERNAL,
            )

        self.assertIn(
            "statement_not_allowed",
            {issue.code for issue in raised.exception.validation.issues},
        )
        self.assertEqual(
            self.repository.list_corrected_sql_examples(
                self.tenant.id,
                self.data_source.id,
            ),
            (),
        )

    def test_explicit_supersession_preserves_history_and_blocks_stale_writer(self) -> None:
        first = self._correct()

        with self.assertRaises(CorrectedSQLConcurrencyError):
            self._correct()

        second = self._correct(
            sql="SELECT total_amount, id FROM public.orders",
            supersedes_example_id=first.example.id,
        )
        history = self.service.list_examples(
            self.tenant.id,
            self.data_source.id,
            include_superseded=True,
        )

        self.assertEqual(second.example.revision, 2)
        self.assertEqual([entry.is_active for entry in history], [False, True])
        with self.assertRaises(CorrectedSQLConcurrencyError):
            self._correct(
                sql="SELECT id FROM public.orders",
                supersedes_example_id=first.example.id,
            )

    def test_retrieval_is_ranked_and_excludes_schema_drift(self) -> None:
        exact = self._correct()
        self.service.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            question="Elenca gli ordini recenti",
            corrected_sql="SELECT id FROM public.orders ORDER BY id DESC",
            actor_id="steward-1",
            content_classification=Classification.INTERNAL,
        )

        matches = self.service.retrieve(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            question="Mostra il valore totale degli ordini",
        )

        self.assertEqual(matches[0].example.id, exact.example.id)
        self.assertEqual(matches[0].score, 1.0)

        CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.data_source.id,
            DataSourceSnapshot(
                data_source_id=self.data_source.id,
                dialect="postgresql",
                objects=(
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="archived_orders",
                        kind=ObjectKind.TABLE,
                        columns=(ColumnSnapshot("id", "bigint", 1, False),),
                    ),
                ),
            ),
        )

        self.assertEqual(
            self.service.retrieve(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                question="Mostra il valore totale degli ordini",
            ),
            (),
        )

    def test_retrieval_cannot_cross_tenant_boundary(self) -> None:
        self._correct()
        other_tenant = self.repository.create_tenant("Other")

        with self.assertRaises(DataSourceNotFoundError):
            self.service.retrieve(
                tenant_id=other_tenant.id,
                data_source_id=self.data_source.id,
                question="Mostra il valore totale degli ordini",
                max_results=0,
            )

    def test_corrected_business_language_drives_context_selection(self) -> None:
        example = self.service.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            question="Qual è il fatturato?",
            corrected_sql="SELECT total_amount FROM public.orders",
            actor_id="steward-1",
            content_classification=Classification.INTERNAL,
        )

        context = ContextBuilderService(self.repository, self.service).build(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="Dimmi il fatturato",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
            target_columns_per_object=1,
        )

        self.assertEqual(context.objects[0].reference, "public.orders")
        self.assertIn(
            f"corrected_sql_example:{example.example.id}",
            context.objects[0].selection_reasons,
        )
        self.assertEqual(context.sql_examples[0].id, example.example.id)
        self.assertEqual(
            context.sql_examples[0].classification,
            Classification.CONFIDENTIAL,
        )

    def _correct(
        self,
        *,
        sql: str = "SELECT id, total_amount FROM public.orders",
        supersedes_example_id: str | None = None,
    ) -> CorrectedSQLExampleEntry:
        return self.service.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            question="Mostra il valore totale degli ordini",
            corrected_sql=sql,
            actor_id="steward-1",
            content_classification=Classification.INTERNAL,
            business_concepts=("gross_order_value",),
            reason="Reviewed against finance policy",
            supersedes_example_id=supersedes_example_id,
        )

    def _ingest_orders_schema(self) -> None:
        CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.data_source.id,
            DataSourceSnapshot(
                data_source_id=self.data_source.id,
                dialect="postgresql",
                objects=(
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="orders",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "bigint", 1, False),
                            ColumnSnapshot(
                                "total_amount",
                                "numeric(18,2)",
                                2,
                                False,
                                classification=Classification.CONFIDENTIAL,
                            ),
                        ),
                    ),
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
