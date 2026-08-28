from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.epistemic import ResolutionAction
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceCapability,
    DataSourceType,
    EpistemicStatus,
    ObjectKind,
    QueryRequest,
    QueryRequestState,
    SemanticDefinition,
)


class CatalogRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.other_tenant = self.repository.create_tenant("Other")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Finance Reporting",
            source_type=DataSourceType.DIRECT_DB,
            dialect="PostgreSQL",
            capabilities={
                DataSourceCapability.INTROSPECT,
                DataSourceCapability.EXECUTE_READ_ONLY,
            },
        )
        self.version = self.repository.create_catalog_version(
            self.tenant.id,
            self.data_source.id,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def definition(
        self,
        description: str,
        status: EpistemicStatus,
        confidence: float,
    ) -> SemanticDefinition:
        return SemanticDefinition(
            tenant_id=self.tenant.id,
            catalog_version_id=self.version.id,
            object_ref="public.orders.total_amount",
            description=description,
            status=status,
            source=f"test:{status.value}",
            confidence=confidence,
        )

    def test_data_source_reads_are_tenant_scoped(self) -> None:
        own = self.repository.get_data_source(self.tenant.id, self.data_source.id)
        foreign = self.repository.get_data_source(self.other_tenant.id, self.data_source.id)

        self.assertIsNotNone(own)
        self.assertIsNone(foreign)
        assert own is not None
        self.assertEqual("postgresql", own.dialect)

    def test_tenant_and_data_source_lists_are_ordered_and_scoped(self) -> None:
        self.assertEqual(
            [self.tenant.id, self.other_tenant.id],
            [tenant.id for tenant in self.repository.list_tenants()],
        )
        second_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Operations",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="oracle",
        )

        self.assertEqual(
            [self.data_source.id, second_source.id],
            [source.id for source in self.repository.list_data_sources(self.tenant.id)],
        )
        self.assertEqual((), self.repository.list_data_sources(self.other_tenant.id))
        with self.assertRaises(LookupError):
            self.repository.list_data_sources("missing-tenant")

    def test_schema_objects_and_columns_are_versioned_and_tenant_scoped(self) -> None:
        orders = self.repository.create_schema_object(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            catalog_version_id=self.version.id,
            schema_name="public",
            name="orders",
            kind=ObjectKind.TABLE,
        )
        amount = self.repository.create_column(
            tenant_id=self.tenant.id,
            schema_object_id=orders.id,
            name="total_amount",
            physical_type="numeric(18,2)",
            ordinal=1,
            nullable=False,
            classification=Classification.CONFIDENTIAL,
        )

        self.assertEqual(
            (orders,),
            self.repository.list_schema_objects(self.tenant.id, self.version.id),
        )
        self.assertEqual((amount,), self.repository.list_columns(self.tenant.id, orders.id))
        self.assertEqual(
            (),
            self.repository.list_schema_objects(self.other_tenant.id, self.version.id),
        )
        self.assertEqual((), self.repository.list_columns(self.other_tenant.id, orders.id))

    def test_inference_cannot_overwrite_confirmed_semantics(self) -> None:
        confirmed = self.repository.propose_semantic_definition(
            self.definition("Gross amount before discounts", EpistemicStatus.CONFIRMED, 1.0)
        )
        inferred = self.repository.propose_semantic_definition(
            self.definition("Net amount after discounts", EpistemicStatus.INFERRED, 0.78)
        )

        self.assertEqual(ResolutionAction.ACCEPT, confirmed.action)
        self.assertEqual(ResolutionAction.KEEP_CURRENT, inferred.action)
        self.assertEqual("Gross amount before discounts", inferred.resolution.description)
        self.assertEqual(EpistemicStatus.CONFIRMED, inferred.resolution.status)

    def test_equal_authority_disagreement_becomes_conflict(self) -> None:
        self.repository.propose_semantic_definition(
            self.definition("Gross order amount", EpistemicStatus.CONFIRMED, 1.0)
        )
        result = self.repository.propose_semantic_definition(
            self.definition("Net order amount", EpistemicStatus.CONFIRMED, 1.0)
        )

        self.assertEqual(ResolutionAction.MARK_CONFLICT, result.action)
        self.assertEqual(EpistemicStatus.CONFLICTING, result.resolution.status)
        self.assertIsNone(result.resolution.selected_definition_id)

    def test_imported_definition_supersedes_inference(self) -> None:
        self.repository.propose_semantic_definition(
            self.definition("Likely order value", EpistemicStatus.INFERRED, 0.55)
        )
        imported = self.repository.propose_semantic_definition(
            self.definition("Order value before tax", EpistemicStatus.IMPORTED, 1.0)
        )

        self.assertEqual(ResolutionAction.ACCEPT, imported.action)
        self.assertEqual(EpistemicStatus.IMPORTED, imported.resolution.status)
        self.assertEqual("Order value before tax", imported.resolution.description)

    def test_audit_log_rejects_mutation(self) -> None:
        events = self.repository.audit_events(self.tenant.id)
        self.assertGreaterEqual(len(events), 3)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(  # noqa: SLF001 - verifies DB enforcement
                "DELETE FROM audit_events WHERE id = ?",
                (events[0].id,),
            )

    def test_query_request_is_tenant_scoped_and_approval_is_audited(self) -> None:
        query_request = self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                catalog_version_id=self.version.id,
                sql_text="SELECT id FROM public.orders",
                normalized_sql="SELECT id FROM public.orders LIMIT 500",
                referenced_tables=("public.orders",),
                referenced_columns=("public.orders.id",),
                validation_issue_codes=("limit_added",),
                state=QueryRequestState.READY_FOR_PREVIEW,
            )
        )

        self.assertEqual(
            query_request,
            self.repository.get_query_request(self.tenant.id, query_request.id),
        )
        self.assertIsNone(
            self.repository.get_query_request(self.other_tenant.id, query_request.id)
        )
        approved = self.repository.transition_query_request(
            self.tenant.id,
            query_request.id,
            QueryRequestState.APPROVED,
            actor_id="reviewer-1",
        )

        self.assertEqual(QueryRequestState.APPROVED, approved.state)
        self.assertEqual("reviewer-1", approved.approved_by)
        events = self.repository.audit_events(self.tenant.id)
        transition = next(
            event
            for event in events
            if event.event_type == "query.state_transitioned"
        )
        self.assertEqual("approved", transition.details["to_state"])
        self.assertNotIn("sql", transition.details)


class LegacySchemaUpgradeTests(unittest.TestCase):
    def test_resolution_scope_is_backfilled_from_selected_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "legacy.sqlite3"
            repository = SQLiteCatalogRepository(catalog_path)
            repository.initialize()
            tenant = repository.create_tenant("Acme")
            data_source = repository.create_data_source(
                tenant_id=tenant.id,
                name="Analytics",
                source_type=DataSourceType.MANUAL_SCHEMA,
                dialect="postgresql",
            )
            version = repository.create_catalog_version(tenant.id, data_source.id)
            repository.propose_semantic_definition(
                SemanticDefinition(
                    tenant_id=tenant.id,
                    catalog_version_id=version.id,
                    object_ref="public.orders",
                    description="Order records",
                    status=EpistemicStatus.IMPORTED,
                    source="legacy:test",
                    confidence=1.0,
                )
            )
            repository.close()

            connection = sqlite3.connect(catalog_path)
            connection.executescript(
                """
                PRAGMA foreign_keys = OFF;
                ALTER TABLE semantic_resolutions RENAME TO semantic_resolutions_current;
                CREATE TABLE semantic_resolutions (
                    tenant_id TEXT NOT NULL,
                    object_ref TEXT NOT NULL,
                    description TEXT NOT NULL,
                    epistemic_status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    selected_definition_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, object_ref)
                );
                INSERT INTO semantic_resolutions
                    (tenant_id, object_ref, description, epistemic_status, confidence,
                     selected_definition_id, updated_at)
                SELECT tenant_id, object_ref, description, epistemic_status, confidence,
                       selected_definition_id, updated_at
                FROM semantic_resolutions_current;
                DROP TABLE semantic_resolutions_current;
                """
            )
            connection.close()

            upgraded = SQLiteCatalogRepository(catalog_path)
            try:
                upgraded.initialize()
                resolution = upgraded.get_semantic_resolution(
                    tenant.id,
                    data_source.id,
                    "public.orders",
                )
                self.assertIsNotNone(resolution)
                assert resolution is not None
                self.assertEqual(data_source.id, resolution.data_source_id)
                columns = {
                    row["name"]
                    for row in upgraded._connection.execute(  # noqa: SLF001
                        "PRAGMA table_info(semantic_resolutions)"
                    )
                }
                self.assertIn("data_source_id", columns)
            finally:
                upgraded.close()


if __name__ == "__main__":
    unittest.main()
