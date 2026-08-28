from __future__ import annotations

from dataclasses import dataclass

from packages.catalog.sqlverity_catalog.explorer import CatalogNotIngestedError
from packages.catalog.sqlverity_catalog.ingestion import DataSourceNotFoundError
from packages.catalog.sqlverity_catalog.repository import (
    LearningGovernanceConflictError,
    SQLiteCatalogRepository,
)
from packages.domain.sqlverity_domain.models import (
    Classification,
    CorrectedSQLExample,
    DataSource,
    GoldenCandidateReview,
    GoldenCandidateStatus,
    GoldenEvaluationCandidate,
    QueryFeedbackEvent,
    QueryFeedbackOutcome,
    QueryRequest,
    QueryRequestState,
)

from .service import LearningLoopService


class FeedbackNotEligibleError(ValueError):
    pass


class FeedbackLinkNotFoundError(LookupError):
    pass


class FeedbackConflictError(RuntimeError):
    pass


class GoldenCandidateNotFoundError(LookupError):
    pass


class GoldenCandidateEligibilityError(ValueError):
    pass


class GoldenCandidateConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FeedbackSummary:
    total_count: int
    accepted_count: int
    rejected_count: int
    corrected_count: int
    acceptance_rate: float | None
    correction_rate: float | None


@dataclass(frozen=True, slots=True)
class GoldenCandidateEntry:
    candidate: GoldenEvaluationCandidate
    status: GoldenCandidateStatus
    review: GoldenCandidateReview | None


@dataclass(frozen=True, slots=True)
class GoldenCandidateExportItem:
    candidate_id: str
    data_source_id: str
    catalog_version_id: str
    corrected_sql_example_id: str
    source_query_request_id: str
    question: str
    dialect: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    assumptions: tuple[str, ...]
    content_classification: Classification


@dataclass(frozen=True, slots=True)
class GoldenCandidateExport:
    format_version: int
    candidates: tuple[GoldenCandidateExportItem, ...]


class LearningGovernanceService:
    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        learning_loop: LearningLoopService,
    ) -> None:
        self._repository = repository
        self._learning_loop = learning_loop

    def record_feedback(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        query_request_id: str,
        outcome: QueryFeedbackOutcome,
        actor_id: str,
        reason: str | None = None,
        corrected_sql_example_id: str | None = None,
    ) -> QueryFeedbackEvent:
        self._require_data_source(tenant_id, data_source_id)
        query_request = self._repository.get_query_request(tenant_id, query_request_id)
        if query_request is None or query_request.data_source_id != data_source_id:
            raise FeedbackLinkNotFoundError(
                "Query request does not exist in this DataSource"
            )
        self._require_feedback_state(query_request, outcome)
        if outcome is QueryFeedbackOutcome.CORRECTED:
            example = self._active_example(
                tenant_id,
                data_source_id,
                corrected_sql_example_id,
            )
            if example.source_query_request_id != query_request_id:
                raise FeedbackNotEligibleError(
                    "Corrected SQL example does not originate from this query request"
                )
        event = QueryFeedbackEvent(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            query_request_id=query_request_id,
            outcome=outcome,
            actor_id=actor_id.strip(),
            reason=reason.strip() if reason is not None else None,
            corrected_sql_example_id=corrected_sql_example_id,
        )
        try:
            return self._repository.create_query_feedback_event(event)
        except LearningGovernanceConflictError as error:
            raise FeedbackConflictError(str(error)) from error

    def summarize_feedback(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> FeedbackSummary:
        self._require_data_source(tenant_id, data_source_id)
        events = self._repository.list_query_feedback_events(tenant_id, data_source_id)
        accepted = sum(
            event.outcome is QueryFeedbackOutcome.ACCEPTED for event in events
        )
        rejected = sum(
            event.outcome is QueryFeedbackOutcome.REJECTED for event in events
        )
        corrected = sum(
            event.outcome is QueryFeedbackOutcome.CORRECTED for event in events
        )
        total = len(events)
        return FeedbackSummary(
            total_count=total,
            accepted_count=accepted,
            rejected_count=rejected,
            corrected_count=corrected,
            acceptance_rate=accepted / total if total else None,
            correction_rate=corrected / total if total else None,
        )

    def promote_candidate(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        corrected_sql_example_id: str,
    ) -> GoldenCandidateEntry:
        self._require_data_source(tenant_id, data_source_id)
        example = self._active_example(
            tenant_id,
            data_source_id,
            corrected_sql_example_id,
        )
        if example.source_query_request_id is None:
            raise GoldenCandidateEligibilityError(
                "Golden candidates require a source query request"
            )
        feedback = self._repository.get_query_feedback_event(
            tenant_id,
            example.source_query_request_id,
        )
        if (
            feedback is None
            or feedback.outcome is not QueryFeedbackOutcome.CORRECTED
            or feedback.corrected_sql_example_id != example.id
        ):
            raise GoldenCandidateEligibilityError(
                "Golden candidates require matching corrected feedback"
            )
        latest_version = self._repository.get_latest_catalog_version(
            tenant_id,
            data_source_id,
        )
        if latest_version is None:
            raise CatalogNotIngestedError("DataSource has no catalog version")
        allowed_tables, allowed_columns = self._catalog_references(
            tenant_id,
            latest_version.id,
        )
        missing_tables = set(example.referenced_tables) - allowed_tables
        missing_columns = set(example.referenced_columns) - allowed_columns
        if missing_tables or missing_columns:
            raise GoldenCandidateEligibilityError(
                "Corrected SQL is not compatible with the current catalog: "
                f"missing tables={sorted(missing_tables)}, "
                f"missing columns={sorted(missing_columns)}"
            )
        candidate = GoldenEvaluationCandidate(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog_version_id=latest_version.id,
            corrected_sql_example_id=example.id,
            source_query_request_id=example.source_query_request_id,
            question=example.question,
            normalized_sql=example.normalized_sql,
            referenced_tables=example.referenced_tables,
            referenced_columns=example.referenced_columns,
            business_concepts=example.business_concepts,
            assumptions=example.assumptions,
            content_classification=example.content_classification,
        )
        try:
            stored = self._repository.create_golden_evaluation_candidate(candidate)
        except LearningGovernanceConflictError as error:
            raise GoldenCandidateConflictError(str(error)) from error
        return GoldenCandidateEntry(
            candidate=stored,
            status=GoldenCandidateStatus.PROPOSED,
            review=None,
        )

    def list_candidates(
        self,
        tenant_id: str,
        data_source_id: str,
        *,
        candidate_status: GoldenCandidateStatus | None = None,
    ) -> tuple[GoldenCandidateEntry, ...]:
        self._require_data_source(tenant_id, data_source_id)
        reviews = {
            review.candidate_id: review
            for review in self._repository.list_golden_candidate_reviews(
                tenant_id,
                data_source_id,
            )
        }
        entries = tuple(
            self._candidate_entry(candidate, reviews.get(candidate.id))
            for candidate in self._repository.list_golden_evaluation_candidates(
                tenant_id,
                data_source_id,
            )
        )
        if candidate_status is None:
            return entries
        return tuple(entry for entry in entries if entry.status is candidate_status)

    def review_candidate(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        candidate_id: str,
        decision: GoldenCandidateStatus,
        actor_id: str,
        reason: str | None = None,
    ) -> GoldenCandidateEntry:
        candidate = self._repository.get_golden_evaluation_candidate(
            tenant_id,
            candidate_id,
        )
        if candidate is None or candidate.data_source_id != data_source_id:
            raise GoldenCandidateNotFoundError(
                "Golden candidate does not exist in this DataSource"
            )
        review = GoldenCandidateReview(
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            status=decision,
            actor_id=actor_id.strip(),
            reason=reason.strip() if reason is not None else None,
        )
        try:
            stored = self._repository.create_golden_candidate_review(review)
        except LearningGovernanceConflictError as error:
            raise GoldenCandidateConflictError(str(error)) from error
        return GoldenCandidateEntry(
            candidate=candidate,
            status=stored.status,
            review=stored,
        )

    def export_approved(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> GoldenCandidateExport:
        data_source = self._require_data_source(tenant_id, data_source_id)
        entries = self.list_candidates(
            tenant_id,
            data_source_id,
            candidate_status=GoldenCandidateStatus.APPROVED,
        )
        items = tuple(
            GoldenCandidateExportItem(
                candidate_id=entry.candidate.id,
                data_source_id=data_source_id,
                catalog_version_id=entry.candidate.catalog_version_id,
                corrected_sql_example_id=entry.candidate.corrected_sql_example_id,
                source_query_request_id=entry.candidate.source_query_request_id,
                question=entry.candidate.question,
                dialect=data_source.dialect,
                normalized_sql=entry.candidate.normalized_sql,
                referenced_tables=entry.candidate.referenced_tables,
                referenced_columns=entry.candidate.referenced_columns,
                business_concepts=entry.candidate.business_concepts,
                assumptions=entry.candidate.assumptions,
                content_classification=entry.candidate.content_classification,
            )
            for entry in sorted(entries, key=lambda item: item.candidate.id)
        )
        return GoldenCandidateExport(format_version=1, candidates=items)

    def _active_example(
        self,
        tenant_id: str,
        data_source_id: str,
        example_id: str | None,
    ) -> CorrectedSQLExample:
        if example_id is None:
            raise FeedbackLinkNotFoundError("Corrected feedback requires an example")
        entries = self._learning_loop.list_examples(tenant_id, data_source_id)
        example = next(
            (entry.example for entry in entries if entry.example.id == example_id),
            None,
        )
        if example is None:
            raise FeedbackLinkNotFoundError(
                "Active corrected SQL example does not exist in this DataSource"
            )
        return example

    def _require_data_source(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> DataSource:
        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        return data_source

    @staticmethod
    def _require_feedback_state(
        query_request: QueryRequest,
        outcome: QueryFeedbackOutcome,
    ) -> None:
        accepted_states = {
            QueryRequestState.READY_FOR_PREVIEW,
            QueryRequestState.APPROVED,
            QueryRequestState.EXECUTING,
            QueryRequestState.SUCCEEDED,
            QueryRequestState.RESULT_PROCESSING,
            QueryRequestState.COMPLETED,
        }
        rejected_states = {QueryRequestState.READY_FOR_PREVIEW, QueryRequestState.REJECTED}
        eligible = (
            accepted_states
            if outcome is QueryFeedbackOutcome.ACCEPTED
            else rejected_states
            if outcome is QueryFeedbackOutcome.REJECTED
            else accepted_states | rejected_states
        )
        if query_request.state not in eligible:
            raise FeedbackNotEligibleError(
                f"Query request state {query_request.state.value} is not eligible for "
                f"{outcome.value} feedback"
            )

    def _catalog_references(
        self,
        tenant_id: str,
        catalog_version_id: str,
    ) -> tuple[set[str], set[str]]:
        schema_objects = self._repository.list_schema_objects(
            tenant_id,
            catalog_version_id,
        )
        tables = {schema_object.reference for schema_object in schema_objects}
        objects_by_id = {schema_object.id: schema_object for schema_object in schema_objects}
        columns = {
            f"{objects_by_id[column.schema_object_id].reference}.{column.name}"
            for column in self._repository.list_columns_for_catalog_version(
                tenant_id,
                catalog_version_id,
            )
        }
        return tables, columns

    @staticmethod
    def _candidate_entry(
        candidate: GoldenEvaluationCandidate,
        review: GoldenCandidateReview | None,
    ) -> GoldenCandidateEntry:
        return GoldenCandidateEntry(
            candidate=candidate,
            status=(review.status if review is not None else GoldenCandidateStatus.PROPOSED),
            review=review,
        )
