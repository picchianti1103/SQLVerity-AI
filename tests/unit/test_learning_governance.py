from __future__ import annotations

import sqlite3
import unittest

from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import (
    Classification,
    CorrectedSQLExample,
    DataSourceType,
    GoldenCandidateStatus,
    ObjectKind,
    QueryFeedbackEvent,
    QueryFeedbackOutcome,
    QueryRequest,
    QueryRequestState,
)
from packages.learning.sqlverity_learning import (
    FeedbackConflictError,
    FeedbackLinkNotFoundError,
    FeedbackNotEligibleError,
    GoldenCandidateConflictError,
    GoldenCandidateEligibilityError,
    GoldenCandidateNotFoundError,
    LearningGovernanceService,
    LearningLoopService,
)
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator


class LearningGovernanceTests(unittest.TestCase):
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
        self.learning_loop = LearningLoopService(
            self.repository,
            PostgreSQLSQLValidator(),
        )
        self.service = LearningGovernanceService(
            self.repository,
            self.learning_loop,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_feedback_is_final_and_produces_real_rates(self) -> None:
        empty = self.service.summarize_feedback(
            self.tenant.id,
            self.data_source.id,
        )
        self.assertEqual(empty.total_count, 0)
        self.assertIsNone(empty.acceptance_rate)
        self.assertIsNone(empty.correction_rate)

        accepted = self._query_request()
        rejected = self._query_request()
        corrected = self._query_request()
        example = self._correct(corrected.id)

        self.service.record_feedback(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=accepted.id,
            outcome=QueryFeedbackOutcome.ACCEPTED,
            actor_id="analyst-1",
        )
        self.service.record_feedback(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=rejected.id,
            outcome=QueryFeedbackOutcome.REJECTED,
            actor_id="analyst-2",
            reason="Wrong business grain",
        )
        correction = self.service.record_feedback(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=corrected.id,
            outcome=QueryFeedbackOutcome.CORRECTED,
            actor_id="analyst-3",
            corrected_sql_example_id=example.id,
        )

        summary = self.service.summarize_feedback(
            self.tenant.id,
            self.data_source.id,
        )
        self.assertEqual(summary.total_count, 3)
        self.assertEqual(summary.accepted_count, 1)
        self.assertEqual(summary.rejected_count, 1)
        self.assertEqual(summary.corrected_count, 1)
        self.assertAlmostEqual(summary.acceptance_rate or 0, 1 / 3)
        self.assertAlmostEqual(summary.correction_rate or 0, 1 / 3)
        audit = next(
            event
            for event in self.repository.audit_events(self.tenant.id)
            if event.event_type == "learning.query_feedback_recorded"
            and event.details["feedback_id"] == correction.id
        )
        self.assertEqual(audit.event_type, "learning.query_feedback_recorded")
        self.assertEqual(audit.details["feedback_id"], correction.id)
        self.assertNotIn("reason", audit.details)

        with self.assertRaises(FeedbackConflictError):
            self.service.record_feedback(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=accepted.id,
                outcome=QueryFeedbackOutcome.REJECTED,
                actor_id="analyst-1",
            )

    def test_feedback_checks_request_state_source_and_active_revision(self) -> None:
        generated = self._query_request(state=QueryRequestState.GENERATED)
        with self.assertRaises(FeedbackNotEligibleError):
            self.service.record_feedback(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=generated.id,
                outcome=QueryFeedbackOutcome.ACCEPTED,
                actor_id="analyst-1",
            )

        first_request = self._query_request()
        other_request = self._query_request()
        first = self._correct(first_request.id)
        with self.assertRaises(FeedbackNotEligibleError):
            self.service.record_feedback(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=other_request.id,
                outcome=QueryFeedbackOutcome.CORRECTED,
                actor_id="analyst-1",
                corrected_sql_example_id=first.id,
            )

        current = self._correct(
            first_request.id,
            sql="SELECT total_amount, id FROM public.orders",
            supersedes_example_id=first.id,
        )
        with self.assertRaises(FeedbackLinkNotFoundError):
            self.service.record_feedback(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=first_request.id,
                outcome=QueryFeedbackOutcome.CORRECTED,
                actor_id="analyst-1",
                corrected_sql_example_id=first.id,
            )
        stored = self.service.record_feedback(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=first_request.id,
            outcome=QueryFeedbackOutcome.CORRECTED,
            actor_id="analyst-1",
            corrected_sql_example_id=current.id,
        )
        self.assertEqual(stored.corrected_sql_example_id, current.id)

    def test_promotion_review_and_deterministic_approved_export(self) -> None:
        request = self._query_request()
        example = self._correct(request.id)
        with self.assertRaises(GoldenCandidateEligibilityError):
            self.service.promote_candidate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                corrected_sql_example_id=example.id,
            )

        self._record_corrected_feedback(request.id, example.id)
        proposed = self.service.promote_candidate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            corrected_sql_example_id=example.id,
        )
        self.assertEqual(proposed.status, GoldenCandidateStatus.PROPOSED)
        self.assertEqual(proposed.candidate.catalog_version_id, self.version_id)
        self.assertIsNone(proposed.review)

        with self.assertRaises(GoldenCandidateConflictError):
            self.service.promote_candidate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                corrected_sql_example_id=example.id,
            )

        approved = self.service.review_candidate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            candidate_id=proposed.candidate.id,
            decision=GoldenCandidateStatus.APPROVED,
            actor_id="reviewer-1",
            reason="Validated against the current schema",
        )
        self.assertEqual(approved.status, GoldenCandidateStatus.APPROVED)

        statements: list[str] = []
        self.repository._connection.set_trace_callback(statements.append)  # noqa: SLF001
        try:
            listed = self.service.list_candidates(
                self.tenant.id,
                self.data_source.id,
            )
        finally:
            self.repository._connection.set_trace_callback(None)  # noqa: SLF001
        self.assertEqual(listed, (approved,))
        selects = tuple(statement.casefold() for statement in statements)
        self.assertEqual(
            1,
            sum("from golden_candidate_reviews" in statement for statement in selects),
        )

        with self.assertRaises(GoldenCandidateConflictError):
            self.service.review_candidate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                candidate_id=proposed.candidate.id,
                decision=GoldenCandidateStatus.REJECTED,
                actor_id="reviewer-2",
            )

        artifact = self.service.export_approved(
            self.tenant.id,
            self.data_source.id,
        )
        self.assertEqual(artifact.format_version, 1)
        self.assertEqual(len(artifact.candidates), 1)
        item = artifact.candidates[0]
        self.assertEqual(item.candidate_id, proposed.candidate.id)
        self.assertEqual(item.dialect, "postgresql")
        self.assertEqual(item.normalized_sql, example.normalized_sql)
        self.assertEqual(
            artifact,
            self.service.export_approved(self.tenant.id, self.data_source.id),
        )

    def test_rejected_and_proposed_candidates_are_not_exported(self) -> None:
        request = self._query_request()
        example = self._correct(request.id)
        self._record_corrected_feedback(request.id, example.id)
        candidate = self.service.promote_candidate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            corrected_sql_example_id=example.id,
        )

        self.assertEqual(
            self.service.export_approved(
                self.tenant.id,
                self.data_source.id,
            ).candidates,
            (),
        )
        self.service.review_candidate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            candidate_id=candidate.candidate.id,
            decision=GoldenCandidateStatus.REJECTED,
            actor_id="reviewer-1",
        )
        self.assertEqual(
            self.service.export_approved(
                self.tenant.id,
                self.data_source.id,
            ).candidates,
            (),
        )

    def test_promotion_blocks_current_schema_drift(self) -> None:
        request = self._query_request()
        example = self._correct(request.id)
        self._record_corrected_feedback(request.id, example.id)
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

        with self.assertRaises(GoldenCandidateEligibilityError) as raised:
            self.service.promote_candidate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                corrected_sql_example_id=example.id,
            )
        self.assertIn("public.orders", str(raised.exception))

    def test_governance_records_are_database_immutable_and_tenant_scoped(self) -> None:
        request = self._query_request()
        example = self._correct(request.id)
        feedback = self._record_corrected_feedback(request.id, example.id)
        candidate = self.service.promote_candidate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            corrected_sql_example_id=example.id,
        )
        reviewed = self.service.review_candidate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            candidate_id=candidate.candidate.id,
            decision=GoldenCandidateStatus.APPROVED,
            actor_id="reviewer-1",
        )
        assert reviewed.review is not None

        mutations = (
            ("UPDATE query_feedback_events SET actor_id = 'x' WHERE id = ?", feedback.id),
            ("DELETE FROM golden_evaluation_candidates WHERE id = ?", candidate.candidate.id),
            ("UPDATE golden_candidate_reviews SET actor_id = 'x' WHERE id = ?", reviewed.review.id),
        )
        for statement, identifier in mutations:
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.repository._connection.execute(statement, (identifier,))  # noqa: SLF001

        other_tenant = self.repository.create_tenant("Other")
        self.assertIsNone(
            self.repository.get_query_feedback_event(other_tenant.id, request.id)
        )
        self.assertIsNone(
            self.repository.get_golden_evaluation_candidate(
                other_tenant.id,
                candidate.candidate.id,
            )
        )
        with self.assertRaises(GoldenCandidateNotFoundError):
            self.service.review_candidate(
                tenant_id=other_tenant.id,
                data_source_id=self.data_source.id,
                candidate_id=candidate.candidate.id,
                decision=GoldenCandidateStatus.APPROVED,
                actor_id="intruder",
            )

    def _query_request(
        self,
        *,
        state: QueryRequestState = QueryRequestState.READY_FOR_PREVIEW,
    ) -> QueryRequest:
        return self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                catalog_version_id=self.version_id,
                sql_text="SELECT id FROM public.orders",
                normalized_sql=(
                    "SELECT id FROM public.orders LIMIT 500"
                    if state is QueryRequestState.READY_FOR_PREVIEW
                    else None
                ),
                referenced_tables=("public.orders",),
                referenced_columns=("public.orders.id",),
                validation_issue_codes=(),
                state=state,
            )
        )

    def _correct(
        self,
        query_request_id: str,
        *,
        sql: str = "SELECT id, total_amount FROM public.orders",
        supersedes_example_id: str | None = None,
    ) -> CorrectedSQLExample:
        return self.learning_loop.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            question="Mostra il valore totale degli ordini",
            corrected_sql=sql,
            actor_id="steward-1",
            content_classification=Classification.INTERNAL,
            business_concepts=("gross_order_value",),
            assumptions=("Only posted orders",),
            source_query_request_id=query_request_id,
            supersedes_example_id=supersedes_example_id,
        ).example

    def _record_corrected_feedback(
        self,
        request_id: str,
        example_id: str,
    ) -> QueryFeedbackEvent:
        return self.service.record_feedback(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=request_id,
            outcome=QueryFeedbackOutcome.CORRECTED,
            actor_id="analyst-1",
            corrected_sql_example_id=example_id,
        )

    def _ingest_orders_schema(self) -> None:
        report = CatalogIngestionService(self.repository, {}).ingest_snapshot(
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
                            ColumnSnapshot("total_amount", "numeric", 2, False),
                        ),
                    ),
                ),
            ),
        )
        self.version_id = report.catalog_version_id


if __name__ == "__main__":
    unittest.main()
