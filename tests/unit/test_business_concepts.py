from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime

from packages.catalog.sqlverity_catalog.business_concepts import (
    BusinessConceptConcurrencyError,
    BusinessConceptCorrectionResult,
    BusinessConceptObjectNotFoundError,
    BusinessConceptService,
    BusinessTermConflictError,
)
from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import (
    BusinessConceptWriteResult,
    SQLiteCatalogRepository,
)
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.epistemic import ResolutionAction
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceType,
    EpistemicStatus,
    ObjectKind,
)
from packages.retrieval.sqlverity_retrieval import ContextBuilderService


class BusinessConceptTests(unittest.TestCase):
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
        self.service = BusinessConceptService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()

    def test_confirmed_synonym_resolves_terms_and_seeds_classified_context(self) -> None:
        correction = self._correct(
            concept_key="gross_revenue",
            name="Gross revenue",
            synonyms=("Fatturato lordo", "Ricavi lordi"),
        )

        resolved = self.service.resolve_terms(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="Mostra il FATTURATO LÓRDO mensile",
        )
        context = ContextBuilderService(
            self.repository,
            business_concepts=self.service,
        ).build(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="Mostra il fatturato lordo mensile",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
            target_columns_per_object=1,
        )

        self.assertEqual("gross_revenue", resolved.matches[0].resolution.concept_key)
        self.assertEqual(("fatturato lordo",), resolved.matches[0].matched_terms)
        self.assertEqual("public.orders", context.objects[0].reference)
        self.assertIn(
            "business_concept:gross_revenue",
            context.objects[0].selection_reasons,
        )
        self.assertEqual("total_amount", context.objects[0].columns[0].name)
        self.assertEqual(Classification.CONFIDENTIAL, context.business_concepts[0].classification)
        self.assertEqual(correction.definition.id, correction.resolution.selected_definition_id)

    def test_lower_authority_evidence_cannot_overwrite_confirmed_concept(self) -> None:
        correction = self._correct(
            concept_key="gross_revenue",
            name="Gross revenue",
            synonyms=("Fatturato",),
        )

        proposed = self.service.propose(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            concept_key="gross_revenue",
            name="Net revenue",
            description="Revenue after discounts",
            synonyms=("Fatturato netto",),
            object_refs=("public.orders.total_amount",),
            content_classification=Classification.INTERNAL,
            status=EpistemicStatus.INFERRED,
            source="llm_inference",
            confidence=0.8,
        )

        self.assertEqual(ResolutionAction.KEEP_CURRENT, proposed.action)
        self.assertEqual(correction.definition.id, proposed.resolution.selected_definition_id)
        self.assertEqual(2, len(self.service.history(
            self.tenant.id,
            self.data_source.id,
            "gross_revenue",
        )))

    def test_equal_inferences_create_review_then_human_correction_resolves_it(self) -> None:
        first = self._propose("Active customer", "Customer with an order")
        second = self._propose("Active customer", "Customer seen in 30 days")

        self.assertEqual(ResolutionAction.ACCEPT, first.action)
        self.assertEqual(ResolutionAction.MARK_CONFLICT, second.action)
        self.assertEqual(EpistemicStatus.CONFLICTING, second.resolution.status)
        self.assertEqual(1, len(self.service.list_review_queue(
            self.tenant.id,
            self.data_source.id,
        )))

        corrected = self._correct(
            concept_key="active_customer",
            name="Active customer",
            synonyms=("Cliente attivo",),
            expected_updated_at=second.resolution.updated_at,
        )
        self.assertEqual(EpistemicStatus.CONFIRMED, corrected.resolution.status)

        with self.assertRaises(BusinessConceptConcurrencyError):
            self._correct(
                concept_key="active_customer",
                name="Active customer",
                synonyms=("Cliente operativo",),
                expected_updated_at=second.resolution.updated_at,
            )

    def test_confirmed_term_collision_is_rejected(self) -> None:
        self._correct(
            concept_key="gross_revenue",
            name="Gross revenue",
            synonyms=("Fatturato",),
        )

        with self.assertRaises(BusinessTermConflictError):
            self._correct(
                concept_key="net_revenue",
                name="Net revenue",
                synonyms=("Fatturato",),
            )

    def test_unknown_object_tenant_isolation_and_immutable_evidence(self) -> None:
        correction = self._correct(
            concept_key="gross_revenue",
            name="Gross revenue",
            synonyms=("Fatturato",),
        )
        with self.assertRaises(BusinessConceptObjectNotFoundError):
            self._correct(
                concept_key="bad_ref",
                name="Bad ref",
                synonyms=(),
                object_refs=("public.missing",),
            )
        self.assertEqual(
            (),
            self.repository.list_business_concept_resolutions(
                self.other_tenant.id,
                self.data_source.id,
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(  # noqa: SLF001
                "UPDATE business_concept_definitions SET source = ? WHERE id = ?",
                ("tampered", correction.definition.id),
            )

    def _correct(
        self,
        *,
        concept_key: str,
        name: str,
        synonyms: tuple[str, ...],
        object_refs: tuple[str, ...] = ("public.orders.total_amount",),
        expected_updated_at: datetime | None = None,
    ) -> BusinessConceptCorrectionResult:
        return self.service.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            concept_key=concept_key,
            name=name,
            description="Governed finance definition",
            synonyms=synonyms,
            object_refs=object_refs,
            content_classification=Classification.INTERNAL,
            actor_id="steward-1",
            expected_updated_at=expected_updated_at,
        )

    def _propose(self, name: str, description: str) -> BusinessConceptWriteResult:
        return self.service.propose(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            concept_key="active_customer",
            name=name,
            description=description,
            synonyms=("Cliente attivo",),
            object_refs=("public.orders",),
            content_classification=Classification.INTERNAL,
            status=EpistemicStatus.INFERRED,
            source="llm_inference",
            confidence=0.7,
        )


if __name__ == "__main__":
    unittest.main()
