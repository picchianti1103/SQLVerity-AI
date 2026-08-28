from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import ExplainResult, ReadOnlyResult
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceCapability,
    DataSourceType,
    ExecutionCostPolicy,
    ObjectKind,
    QueryParameterDefinition,
    QueryParameterType,
    QueryRequest,
    QueryRequestState,
)
from packages.llm_gateway.sqlverity_llm_gateway import (
    MetadataOnlyPolicyEngine,
    SchemaQuestionPolicyEngine,
)
from packages.query.sqlverity_query import (
    QueryExecutionPolicyBlockedError,
    QueryExecutionService,
    QueryExecutionStaleError,
    QueryExecutionStateError,
    QueryExecutionValidationError,
)
from packages.result_engine.sqlverity_result_engine import DeterministicResultProcessor
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator, SQLValidatorRegistry


class FakeReadOnlyExecutor:
    def __init__(self) -> None:
        self.explain_calls: list[str] = []
        self.execute_calls: list[str] = []
        self.cancelled: list[str] = []
        self.explain_parameters: list[Mapping[str, Any]] = []
        self.execute_parameters: list[Mapping[str, Any]] = []
        self.fail_execution = False

    def explain(
        self,
        data_source: Any,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> ExplainResult:
        self.explain_calls.append(sql)
        self.explain_parameters.append(dict(parameters))
        return ExplainResult(
            plan={"Plan": {"Node Type": "Seq Scan", "Total Cost": 8.5}},
            estimated_total_cost=8.5,
            estimated_rows=2,
            elapsed_ms=3,
        )

    def execute_read_only(
        self,
        data_source: Any,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
        max_rows: int,
        max_result_bytes: int,
    ) -> ReadOnlyResult:
        self.execute_calls.append(sql)
        self.execute_parameters.append(dict(parameters))
        if self.fail_execution:
            raise RuntimeError("database unavailable")
        return ReadOnlyResult(
            columns=("id",),
            rows=({"id": 1}, {"id": 2}),
            row_count=2,
            truncated=False,
            truncation_reason=None,
            result_bytes=16,
            elapsed_ms=4,
        )

    def cancel(self, request_id: str) -> bool:
        self.cancelled.append(request_id)
        return True


class QueryExecutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
            capabilities={
                DataSourceCapability.EXPLAIN,
                DataSourceCapability.EXECUTE_READ_ONLY,
                DataSourceCapability.CANCEL,
            },
            connection_secret_ref="vault://analytics",
        )
        self.version = self.repository.create_catalog_version(
            self.tenant.id,
            self.data_source.id,
        )
        orders = self.repository.create_schema_object(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            catalog_version_id=self.version.id,
            schema_name="public",
            name="orders",
            kind=ObjectKind.TABLE,
        )
        self.repository.create_column(
            tenant_id=self.tenant.id,
            schema_object_id=orders.id,
            name="id",
            physical_type="bigint",
            ordinal=1,
            nullable=False,
            classification=Classification.CONFIDENTIAL,
        )
        self.executor = FakeReadOnlyExecutor()
        self.service = QueryExecutionService(
            self.repository,
            PostgreSQLSQLValidator(),
            SchemaQuestionPolicyEngine(),
            {"postgresql": self.executor},
            DeterministicResultProcessor(),
            max_rows=2,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_explain_is_available_before_approval_and_is_audited(self) -> None:
        query_request = self._request()

        result = self.service.explain(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=query_request.id,
        )

        self.assertEqual(8.5, result.estimated_total_cost)
        self.assertEqual(["SELECT id FROM public.orders LIMIT 2"], self.executor.explain_calls)
        events = self.repository.audit_events(self.tenant.id)
        explained = next(event for event in events if event.event_type == "query.explained")
        self.assertEqual(8.5, explained.details["estimated_total_cost"])
        self.assertNotIn("plan", explained.details)

    def test_execution_requires_approval_then_completes_lifecycle(self) -> None:
        query_request = self._request()
        with self.assertRaises(QueryExecutionStateError):
            self.service.execute(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=query_request.id,
            )

        self.service.explain(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=query_request.id,
        )
        approved = self.service.approve(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=query_request.id,
            actor_id="reviewer-1",
        )
        run = self.service.execute(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=approved.id,
        )

        self.assertEqual(QueryRequestState.APPROVED, approved.state)
        self.assertEqual("reviewer-1", approved.approved_by)
        self.assertEqual(QueryRequestState.COMPLETED, run.query_request.state)
        self.assertEqual(2, run.result.row_count)
        self.assertEqual("table", run.answer.shape)
        self.assertFalse(run.privacy.raw_rows_sent_to_llm)
        self.assertEqual("deterministic_local", run.privacy.processing_mode)
        self.assertEqual(Classification.CONFIDENTIAL, run.privacy.maximum_classification)
        self.assertEqual("[REDACTED]", run.result.rows[0]["id"])
        self.assertEqual(8.5, run.provenance.estimated_db_cost)
        events = self.repository.audit_events(self.tenant.id)
        result_event = next(
            event for event in events if event.event_type == "query.result_metadata"
        )
        self.assertEqual(2, result_event.details["row_count"])
        self.assertNotIn("rows", result_event.details)
        self.assertFalse(result_event.details["raw_rows_sent_to_llm"])

    def test_parameter_values_are_typed_and_bound_across_the_lifecycle(self) -> None:
        definition = QueryParameterDefinition(
            name="order_id",
            value_type=QueryParameterType.INTEGER,
        )
        request = self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                catalog_version_id=self.version.id,
                sql_text="SELECT id FROM public.orders WHERE id = :order_id",
                normalized_sql=(
                    "SELECT id FROM public.orders WHERE id = %(order_id)s LIMIT 2"
                ),
                referenced_tables=("public.orders",),
                referenced_columns=("public.orders.id",),
                validation_issue_codes=("limit_added",),
                state=QueryRequestState.READY_FOR_PREVIEW,
                parameter_definitions=(definition,),
                parameter_names=("order_id",),
            )
        )

        self.service.explain(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=request.id,
            parameters={"order_id": 1},
        )
        with self.assertRaises(QueryExecutionPolicyBlockedError):
            self.service.approve(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=request.id,
                actor_id="reviewer-1",
                parameters={"order_id": 2},
            )
        approved = self.service.approve(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=request.id,
            actor_id="reviewer-1",
            parameters={"order_id": 1},
        )
        self.service.execute(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=approved.id,
            parameters={"order_id": 1},
        )

        self.assertEqual({"order_id": 1}, self.executor.explain_parameters[-1])
        self.assertEqual({"order_id": 1}, self.executor.execute_parameters[-1])
        events = self.repository.audit_events(self.tenant.id)
        explained = next(event for event in events if event.event_type == "query.explained")
        self.assertEqual(["order_id"], explained.details["parameter_names"])
        self.assertNotIn("parameter_value_hash", explained.details)

    def test_execution_failure_moves_request_to_failed_execution(self) -> None:
        query_request = self._request()
        self.service.approve(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=query_request.id,
            actor_id="reviewer-1",
        )
        self.executor.fail_execution = True

        with self.assertRaises(RuntimeError):
            self.service.execute(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=query_request.id,
            )

        failed = self.repository.get_query_request(self.tenant.id, query_request.id)
        assert failed is not None
        self.assertEqual(QueryRequestState.FAILED_EXECUTION, failed.state)

    def test_catalog_change_invalidates_pending_request(self) -> None:
        query_request = self._request()
        self.repository.create_catalog_version(self.tenant.id, self.data_source.id)

        with self.assertRaises(QueryExecutionStaleError):
            self.service.approve(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=query_request.id,
                actor_id="reviewer-1",
            )

    def test_sql_access_policy_can_block_before_database_call(self) -> None:
        query_request = self._request()
        blocked_service = QueryExecutionService(
            self.repository,
            PostgreSQLSQLValidator(),
            MetadataOnlyPolicyEngine(),
            {"postgresql": self.executor},
            DeterministicResultProcessor(),
            max_rows=2,
        )

        with self.assertRaises(QueryExecutionPolicyBlockedError):
            blocked_service.explain(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=query_request.id,
            )

        self.assertEqual([], self.executor.explain_calls)

    def test_execution_cost_policy_requires_explain_and_enforces_thresholds(self) -> None:
        query_request = self._request()
        self.repository.upsert_execution_cost_policy(
            ExecutionCostPolicy(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                max_total_cost=5,
                max_estimated_rows=10,
            )
        )

        with self.assertRaisesRegex(QueryExecutionPolicyBlockedError, "requires EXPLAIN"):
            self.service.approve(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=query_request.id,
                actor_id="reviewer-1",
            )

        self.service.explain(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=query_request.id,
        )
        with self.assertRaisesRegex(QueryExecutionPolicyBlockedError, "Planner cost exceeds"):
            self.service.approve(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=query_request.id,
                actor_id="reviewer-1",
            )

        updated = self.repository.upsert_execution_cost_policy(
            ExecutionCostPolicy(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                max_total_cost=10,
                max_estimated_rows=10,
            )
        )
        approved = self.service.approve(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=query_request.id,
            actor_id="reviewer-1",
        )

        self.assertEqual(QueryRequestState.APPROVED, approved.state)
        self.assertEqual(10, updated.max_total_cost)

    def test_missing_catalog_classification_blocks_before_database_call(self) -> None:
        query_request = self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                catalog_version_id=self.version.id,
                sql_text="SELECT missing FROM public.orders",
                normalized_sql="SELECT missing FROM public.orders LIMIT 2",
                referenced_tables=("public.orders",),
                referenced_columns=("public.orders.missing",),
                validation_issue_codes=(),
                state=QueryRequestState.READY_FOR_PREVIEW,
            )
        )

        with self.assertRaises(QueryExecutionValidationError):
            self.service.explain(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=query_request.id,
            )

        self.assertEqual([], self.executor.explain_calls)

    def test_pending_and_active_requests_can_be_cancelled(self) -> None:
        pending = self._request()
        cancelled_pending = self.service.cancel(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=pending.id,
        )
        self.assertEqual(QueryRequestState.CANCELLED, cancelled_pending.state)

        active = self._request()
        self.service.approve(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=active.id,
            actor_id="reviewer-1",
        )
        self.repository.transition_query_request(
            self.tenant.id,
            active.id,
            QueryRequestState.EXECUTING,
        )
        cancelled_active = self.service.cancel(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=active.id,
        )

        self.assertEqual(QueryRequestState.CANCELLED, cancelled_active.state)
        self.assertEqual([active.id], self.executor.cancelled)

    def test_mysql_request_is_revalidated_and_explained_in_its_own_dialect(self) -> None:
        mysql_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="MySQL analytics",
            source_type=DataSourceType.DIRECT_DB,
            dialect="mysql",
            capabilities={
                DataSourceCapability.EXPLAIN,
                DataSourceCapability.EXECUTE_READ_ONLY,
            },
            connection_secret_ref="vault://mysql",
        )
        version = self.repository.create_catalog_version(self.tenant.id, mysql_source.id)
        orders = self.repository.create_schema_object(
            tenant_id=self.tenant.id,
            data_source_id=mysql_source.id,
            catalog_version_id=version.id,
            schema_name="analytics",
            name="orders",
            kind=ObjectKind.TABLE,
        )
        self.repository.create_column(
            tenant_id=self.tenant.id,
            schema_object_id=orders.id,
            name="id",
            physical_type="bigint",
            ordinal=1,
            nullable=False,
            classification=Classification.INTERNAL,
        )
        request = self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=mysql_source.id,
                catalog_version_id=version.id,
                sql_text="SELECT `id` FROM `analytics`.`orders`",
                normalized_sql="SELECT `id` FROM `analytics`.`orders` LIMIT 2",
                referenced_tables=("analytics.orders",),
                referenced_columns=("analytics.orders.id",),
                validation_issue_codes=("limit_added",),
                state=QueryRequestState.READY_FOR_PREVIEW,
            )
        )
        executor = FakeReadOnlyExecutor()
        service = QueryExecutionService(
            self.repository,
            SQLValidatorRegistry(),
            SchemaQuestionPolicyEngine(),
            {"mysql": executor},
            DeterministicResultProcessor(),
            max_rows=2,
        )

        result = service.explain(
            tenant_id=self.tenant.id,
            data_source_id=mysql_source.id,
            request_id=request.id,
        )

        self.assertEqual(8.5, result.estimated_total_cost)
        self.assertEqual(1, len(executor.explain_calls))
        self.assertIn("`analytics`.`orders`", executor.explain_calls[0])

    def _request(self) -> QueryRequest:
        return self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                catalog_version_id=self.version.id,
                sql_text="SELECT id FROM public.orders",
                normalized_sql="SELECT id FROM public.orders LIMIT 2",
                referenced_tables=("public.orders",),
                referenced_columns=("public.orders.id",),
                validation_issue_codes=("limit_added",),
                state=QueryRequestState.READY_FOR_PREVIEW,
            )
        )


if __name__ == "__main__":
    unittest.main()
