from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime

from packages.catalog.sqlverity_catalog.analytics_semantics import (
    AnalyticSemanticConcurrencyError,
    AnalyticSemanticNameConflictError,
    AnalyticSemanticReferenceError,
    AnalyticSemanticValidationError,
    AnalyticsSemanticsService,
)
from packages.catalog.sqlverity_catalog.business_concepts import BusinessConceptService
from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import (
    AnalyticSemanticWriteResult,
    SQLiteCatalogRepository,
)
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.epistemic import ResolutionAction
from packages.domain.sqlverity_domain.models import (
    AnalyticSemanticKind,
    BusinessRuleResolution,
    Classification,
    DataSourceType,
    EpistemicStatus,
    MetricResolution,
    ObjectKind,
)
from packages.retrieval.sqlverity_retrieval import ContextBuilderService


class AnalyticsSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.other_tenant = self.repository.create_tenant("Other")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
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
                            ColumnSnapshot("status", "text", 2, False),
                            ColumnSnapshot(
                                "total_amount",
                                "numeric(18,2)",
                                3,
                                False,
                                classification=Classification.CONFIDENTIAL,
                            ),
                            ColumnSnapshot("created_at", "timestamp", 4, False),
                        ),
                    ),
                ),
            ),
        )
        self.concepts = BusinessConceptService(self.repository)
        self.concepts.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            concept_key="gross_revenue",
            name="Gross revenue",
            description="Gross booked value",
            synonyms=("Fatturato",),
            object_refs=("public.orders.total_amount",),
            content_classification=Classification.INTERNAL,
            actor_id="steward",
        )
        self.service = AnalyticsSemanticsService(self.repository, self.concepts)

    def tearDown(self) -> None:
        self.repository.close()

    def test_confirmed_rule_and_metric_retain_validated_dependencies(self) -> None:
        rule = self._correct_rule()
        metric = self._correct_metric()
        self.assertIsInstance(rule.resolution, BusinessRuleResolution)
        self.assertIsInstance(metric.resolution, MetricResolution)
        assert isinstance(rule.resolution, BusinessRuleResolution)
        assert isinstance(metric.resolution, MetricResolution)

        self.assertEqual(
            "public.orders.status = 'paid'",
            rule.resolution.normalized_predicate_sql,
        )
        self.assertEqual(
            "SUM(public.orders.total_amount)",
            metric.resolution.normalized_expression_sql,
        )
        self.assertEqual(("valid_order",), metric.resolution.rule_keys)
        self.assertEqual(("public.orders.id",), metric.resolution.grain_refs)
        self.assertEqual(
            (
                "public.orders.created_at",
                "public.orders.id",
                "public.orders.total_amount",
            ),
            metric.resolution.object_refs,
        )

        concept_match = self.concepts.resolve_terms(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="Mostra il fatturato",
        )
        context = self.service.resolve_for_query(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="Mostra il fatturato",
            concept_keys=frozenset(
                item.resolution.concept_key for item in concept_match.matches
            ),
        )
        self.assertEqual("gross_revenue", context.metrics[0].resolution.metric_key)
        self.assertEqual("valid_order", context.business_rules[0].resolution.rule_key)
        self.assertEqual(("gross_revenue",), context.business_rules[0].selected_by_metrics)

        schema_context = ContextBuilderService(
            self.repository,
            business_concepts=self.concepts,
            analytics_semantics=self.service,
        ).build(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="Mostra il fatturato",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
            target_columns_per_object=1,
        )
        self.assertEqual("public.orders", schema_context.objects[0].reference)
        self.assertIn("metric:gross_revenue", schema_context.objects[0].selection_reasons)
        self.assertEqual(
            Classification.CONFIDENTIAL,
            schema_context.metrics[0].classification,
        )
        self.assertEqual("valid_order", schema_context.business_rules[0].rule_key)

    def test_fragments_fail_closed(self) -> None:
        invalid_metrics = (
            "public.orders.total_amount",
            "SUM(total_amount)",
            "SUM(public.orders.*)",
            "SUM(public.orders.total_amount); DELETE FROM public.orders",
            "SUM(public.orders.total_amount) FILTER (WHERE public.orders.status = 'paid')",
            "dblink('x')",
        )
        for expression in invalid_metrics:
            with self.subTest(expression=expression):
                with self.assertRaises(AnalyticSemanticValidationError):
                    self._correct_metric(expression_sql=expression, rule_keys=())

        with self.assertRaises(AnalyticSemanticValidationError):
            self._correct_rule(predicate_sql="public.orders.status")
        with self.assertRaises(AnalyticSemanticReferenceError):
            self._correct_rule(predicate_sql="public.orders.missing = 1")

    def test_lower_evidence_cannot_overwrite_confirmation(self) -> None:
        confirmed = self._correct_rule()
        proposed = self.service.propose_business_rule(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            rule_key="valid_order",
            name="Valid order",
            description="Unreviewed alternative",
            predicate_sql="public.orders.status = 'shipped'",
            concept_keys=("gross_revenue",),
            content_classification=Classification.INTERNAL,
            status=EpistemicStatus.INFERRED,
            source="llm_inference",
            confidence=0.7,
        )

        self.assertEqual(ResolutionAction.KEEP_CURRENT, proposed.action)
        self.assertEqual(
            confirmed.evidence.id,
            proposed.resolution.selected_definition_id,
        )

    def test_equal_inferences_conflict_and_stale_correction_is_rejected(self) -> None:
        first = self._propose_metric("SUM(public.orders.total_amount)")
        second = self._propose_metric("AVG(public.orders.total_amount)")
        self.assertEqual(ResolutionAction.ACCEPT, first.action)
        self.assertEqual(ResolutionAction.MARK_CONFLICT, second.action)
        self.assertEqual(1, len(self.service.list_review_queue(
            self.tenant.id,
            self.data_source.id,
        )))

        corrected = self._correct_metric(
            rule_keys=(),
            expected_updated_at=second.resolution.updated_at,
        )
        self.assertEqual(EpistemicStatus.CONFIRMED, corrected.resolution.status)
        with self.assertRaises(AnalyticSemanticConcurrencyError):
            self._correct_metric(
                expression_sql="AVG(public.orders.total_amount)",
                rule_keys=(),
                expected_updated_at=second.resolution.updated_at,
            )

    def test_confirmed_name_collision_is_rejected(self) -> None:
        self._correct_rule()
        with self.assertRaises(AnalyticSemanticNameConflictError):
            self.service.correct_business_rule(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                rule_key="billable_order",
                name="Valid order",
                description="Second meaning",
                predicate_sql="public.orders.status = 'paid'",
                concept_keys=(),
                content_classification=Classification.INTERNAL,
                actor_id="steward",
            )

    def test_evidence_is_immutable_and_tenant_scoped(self) -> None:
        rule = self._correct_rule()
        self.assertEqual(
            (),
            self.repository.list_analytic_semantic_resolutions(
                self.other_tenant.id,
                self.data_source.id,
                kind=AnalyticSemanticKind.BUSINESS_RULE,
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(  # noqa: SLF001
                "UPDATE analytic_semantic_definitions SET source = ? WHERE id = ?",
                ("tampered", rule.evidence.id),
            )

    def _correct_rule(
        self,
        *,
        predicate_sql: str = "public.orders.status = 'paid'",
    ) -> AnalyticSemanticWriteResult:
        return self.service.correct_business_rule(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            rule_key="valid_order",
            name="Valid order",
            description="Only paid orders are valid",
            predicate_sql=predicate_sql,
            concept_keys=("gross_revenue",),
            content_classification=Classification.INTERNAL,
            actor_id="steward",
        )

    def _correct_metric(
        self,
        *,
        expression_sql: str = "SUM(public.orders.total_amount)",
        rule_keys: tuple[str, ...] = ("valid_order",),
        expected_updated_at: datetime | None = None,
    ) -> AnalyticSemanticWriteResult:
        return self.service.correct_metric(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            metric_key="gross_revenue",
            name="Gross revenue",
            description="Sum of valid gross order value",
            expression_sql=expression_sql,
            grain_refs=("public.orders.id",),
            dimension_refs=("public.orders.created_at",),
            concept_keys=("gross_revenue",),
            rule_keys=rule_keys,
            content_classification=Classification.INTERNAL,
            actor_id="steward",
            expected_updated_at=expected_updated_at,
        )

    def _propose_metric(self, expression_sql: str) -> AnalyticSemanticWriteResult:
        return self.service.propose_metric(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            metric_key="gross_revenue",
            name="Gross revenue",
            description="Candidate metric",
            expression_sql=expression_sql,
            grain_refs=("public.orders.id",),
            dimension_refs=(),
            concept_keys=("gross_revenue",),
            rule_keys=(),
            content_classification=Classification.INTERNAL,
            status=EpistemicStatus.INFERRED,
            source="llm_inference",
            confidence=0.7,
        )


if __name__ == "__main__":
    unittest.main()
