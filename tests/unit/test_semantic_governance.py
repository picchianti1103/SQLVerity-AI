from __future__ import annotations

import sqlite3
import unittest

from packages.catalog.sqlverity_catalog.governance import (
    SemanticConcurrencyError,
    SemanticGovernanceService,
    SemanticObjectNotFoundError,
)
from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import (
    SemanticWriteResult,
    SQLiteCatalogRepository,
)
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import (
    DataSourceType,
    EpistemicStatus,
    ObjectKind,
    SemanticDefinition,
)


class SemanticGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source_id, self.catalog_version_id = self._create_source("Analytics")
        self.governance = SemanticGovernanceService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()

    def test_inferred_semantics_are_listed_for_review(self) -> None:
        result = self._propose_inference(self.data_source_id, self.catalog_version_id)

        queue = self.governance.list_review_queue(
            self.tenant.id,
            self.data_source_id,
        )

        self.assertEqual(1, len(queue))
        self.assertEqual("public.orders", queue[0].object_ref)
        self.assertEqual(EpistemicStatus.INFERRED, queue[0].status)
        self.assertEqual(result.evidence.id, queue[0].evidence[0].id)
        self.assertTrue(queue[0].evidence[0].selected)

    def test_human_confirmation_preserves_history_and_clears_review(self) -> None:
        inferred = self._propose_inference(self.data_source_id, self.catalog_version_id)

        corrected = self.governance.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source_id,
            object_ref="public.orders",
            actor_id="steward-1",
            description=None,
            reason="Reviewed against the glossary",
            expected_updated_at=inferred.resolution.updated_at,
        )

        self.assertEqual(EpistemicStatus.CONFIRMED, corrected.resolution.status)
        self.assertEqual("steward-1", corrected.definition.actor_id)
        self.assertEqual("Reviewed against the glossary", corrected.definition.reason)
        self.assertEqual((), self.governance.list_review_queue(self.tenant.id, self.data_source_id))
        history = self.governance.history(
            self.tenant.id,
            self.data_source_id,
            "public.orders",
        )
        self.assertEqual(2, len(history))
        self.assertEqual(1, sum(item.selected for item in history))
        self.assertEqual(
            corrected.definition.id,
            next(item.id for item in history if item.selected),
        )

    def test_explicit_correction_can_supersede_confirmed_semantics(self) -> None:
        first = self.governance.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source_id,
            object_ref="public.orders",
            actor_id="steward-1",
            description="Commercial orders",
            expected_updated_at=None,
        )
        second = self.governance.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source_id,
            object_ref="public.orders",
            actor_id="steward-2",
            description="Approved commercial orders",
            reason="Clarified approval state",
            expected_updated_at=first.resolution.updated_at,
        )

        self.assertEqual(EpistemicStatus.CONFIRMED, second.resolution.status)
        self.assertEqual("Approved commercial orders", second.resolution.description)
        self.assertEqual(
            2,
            len(
                self.governance.history(
                    self.tenant.id,
                    self.data_source_id,
                    "public.orders",
                )
            ),
        )

    def test_stale_or_missing_version_is_rejected(self) -> None:
        inferred = self._propose_inference(self.data_source_id, self.catalog_version_id)
        with self.assertRaises(SemanticConcurrencyError):
            self.governance.correct(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source_id,
                object_ref="public.orders",
                actor_id="steward-1",
                description="Orders",
            )

        current = self.governance.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source_id,
            object_ref="public.orders",
            actor_id="steward-1",
            description="Orders",
            expected_updated_at=inferred.resolution.updated_at,
        )
        self.assertNotEqual(inferred.resolution.updated_at, current.resolution.updated_at)
        with self.assertRaises(SemanticConcurrencyError):
            self.governance.correct(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source_id,
                object_ref="public.orders",
                actor_id="steward-2",
                description="Changed orders",
                expected_updated_at=inferred.resolution.updated_at,
            )

    def test_semantic_evidence_is_immutable_in_storage(self) -> None:
        inferred = self._propose_inference(self.data_source_id, self.catalog_version_id)

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(
                "UPDATE semantic_definitions SET description = ? WHERE id = ?",
                ("Changed in place", inferred.evidence.id),
            )

        history = self.governance.history(
            self.tenant.id,
            self.data_source_id,
            "public.orders",
        )
        self.assertEqual("Orders inferred by the model", history[0].description)

    def test_same_reference_is_isolated_between_data_sources(self) -> None:
        other_source_id, _ = self._create_source("ERP")
        self._propose_inference(self.data_source_id, self.catalog_version_id)

        self.assertIsNone(
            self.repository.get_semantic_resolution(
                self.tenant.id,
                other_source_id,
                "public.orders",
            )
        )
        corrected = self.governance.correct(
            tenant_id=self.tenant.id,
            data_source_id=other_source_id,
            object_ref="public.orders",
            actor_id="erp-steward",
            description="ERP orders",
            expected_updated_at=None,
        )

        self.assertEqual("ERP orders", corrected.resolution.description)
        self.assertEqual(
            1,
            len(
                self.governance.history(
                    self.tenant.id,
                    other_source_id,
                    "public.orders",
                )
            ),
        )
        self.assertEqual(
            1,
            len(
                self.governance.history(
                    self.tenant.id,
                    self.data_source_id,
                    "public.orders",
                )
            ),
        )

    def test_unknown_reference_cannot_be_corrected(self) -> None:
        with self.assertRaises(SemanticObjectNotFoundError):
            self.governance.correct(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source_id,
                object_ref="public.missing",
                actor_id="steward-1",
                description="Missing object",
            )

    def _create_source(self, name: str) -> tuple[str, str]:
        data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name=name,
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        report = CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            data_source.id,
            DataSourceSnapshot(
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
            ),
        )
        return data_source.id, report.catalog_version_id

    def _propose_inference(
        self,
        data_source_id: str,
        catalog_version_id: str,
    ) -> SemanticWriteResult:
        result = self.repository.propose_semantic_definition(
            SemanticDefinition(
                tenant_id=self.tenant.id,
                catalog_version_id=catalog_version_id,
                object_ref="public.orders",
                description="Orders inferred by the model",
                status=EpistemicStatus.INFERRED,
                source="llm:test-model",
                confidence=0.72,
            )
        )
        self.assertEqual(data_source_id, result.resolution.data_source_id)
        return result


if __name__ == "__main__":
    unittest.main()
