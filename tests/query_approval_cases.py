from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, TypedDict
from unittest.mock import patch
from uuid import uuid4

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
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
from packages.llm_gateway.sqlverity_llm_gateway import SchemaQuestionPolicyEngine
from packages.query.sqlverity_query import (
    QueryExecutionPolicyBlockedError,
    QueryExecutionService,
    QueryExecutionStaleError,
    QueryExecutionStateError,
)
from packages.result_engine.sqlverity_result_engine import DeterministicResultProcessor
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator
from tests.unit.test_query_execution import FakeReadOnlyExecutor


class QueryScope(TypedDict):
    tenant_id: str
    data_source_id: str
    request_id: str


class QueryApprovalCases(unittest.TestCase):
    """Same race scenarios against two independent connections on either backend."""

    def repositories(self) -> tuple[SQLiteCatalogRepository, SQLiteCatalogRepository]:
        raise NotImplementedError

    def setUp(self) -> None:
        self.repository, self.other = self.repositories()
        self.addCleanup(self.repository.close)
        self.addCleanup(self.other.close)
        self.repository.initialize()
        tenant = self.repository.create_tenant(f"Approval regression {uuid4()}")
        source = self.repository.create_data_source(
            tenant_id=tenant.id,
            name="Synthetic source",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
            connection_secret_ref="vault://synthetic",
            capabilities={DataSourceCapability.EXPLAIN, DataSourceCapability.EXECUTE_READ_ONLY},
        )
        version = self.repository.create_catalog_version(tenant.id, source.id)
        table = self.repository.create_schema_object(
            tenant_id=tenant.id,
            data_source_id=source.id,
            catalog_version_id=version.id,
            schema_name="public",
            name="orders",
            kind=ObjectKind.TABLE,
        )
        self.repository.create_column(
            tenant_id=tenant.id,
            schema_object_id=table.id,
            name="id",
            physical_type="integer",
            ordinal=1,
            nullable=False,
            classification=Classification.INTERNAL,
        )
        request = self.repository.create_query_request(
            QueryRequest(
                tenant_id=tenant.id,
                data_source_id=source.id,
                catalog_version_id=version.id,
                sql_text="SELECT id FROM orders WHERE id = :order_id",
                normalized_sql="SELECT id FROM public.orders WHERE id = %(order_id)s LIMIT 2",
                referenced_tables=("public.orders",),
                referenced_columns=("public.orders.id",),
                validation_issue_codes=(),
                state=QueryRequestState.READY_FOR_PREVIEW,
                parameter_definitions=(
                    QueryParameterDefinition(
                        name="order_id",
                        value_type=QueryParameterType.INTEGER,
                    ),
                ),
                parameter_names=("order_id",),
            )
        )
        self.args: QueryScope = dict(
            tenant_id=tenant.id,
            data_source_id=source.id,
            request_id=request.id,
        )
        self.executor = FakeReadOnlyExecutor()
        self.service = self.make_service(self.repository)
        self.other_service = self.make_service(self.other)

    def make_service(self, repository: SQLiteCatalogRepository) -> QueryExecutionService:
        return QueryExecutionService(
            repository,
            PostgreSQLSQLValidator(),
            SchemaQuestionPolicyEngine(),
            {"postgresql": self.executor},
            DeterministicResultProcessor(),
            max_rows=2,
        )

    def current(self) -> QueryRequest:
        current = self.repository.get_query_request(self.args["tenant_id"], self.args["request_id"])
        assert current is not None
        return current

    def test_late_explain_cannot_replace_approved_parameters(self) -> None:
        first = self.service.explain(**self.args, parameters={"order_id": 1})
        started, release = Event(), Event()
        original = self.executor.explain

        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            started.set()
            if not release.wait(10):
                raise TimeoutError("EXPLAIN synchronization timed out")
            return result

        with patch.object(self.executor, "explain", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    self.other_service.explain, **self.args, parameters={"order_id": 2}
                )
                try:
                    self.assertTrue(started.wait(10))
                    approved = self.service.approve(
                        **self.args,
                        actor_id="reviewer",
                        parameters={"order_id": 1},
                        expected_explain_revision=first.revision,
                    )
                finally:
                    release.set()
                with self.assertRaises(QueryExecutionStaleError):
                    pending.result(timeout=10)
        self.assertEqual(approved, self.current())
        self.assertEqual(1, approved.approved_explain_revision)
        self.assertEqual(approved.parameter_value_hash, approved.approved_parameter_value_hash)
        with self.assertRaises(QueryExecutionPolicyBlockedError):
            self.service.execute(**self.args, parameters={"order_id": 2})
        with self.assertRaises(QueryExecutionStateError):
            self.service.explain(**self.args, parameters={"order_id": 1})
        run = self.service.execute(**self.args, parameters={"order_id": 1})
        self.assertEqual(QueryRequestState.COMPLETED, run.query_request.state)
        self.assertEqual([{"order_id": 1}], self.executor.execute_parameters)
        events = self.repository.audit_events(self.args["tenant_id"])
        self.assertEqual(1, sum(event.event_type == "query.explained" for event in events))

    def test_explain_completed_during_approval_invalidates_the_read_revision(self) -> None:
        self.service.explain(**self.args, parameters={"order_id": 1})
        started, release = Event(), Event()
        original = self.repository.transition_query_request

        def delayed(*args: Any, **kwargs: Any) -> QueryRequest:
            started.set()
            if not release.wait(10):
                raise TimeoutError("Approval synchronization timed out")
            return original(*args, **kwargs)

        with patch.object(self.repository, "transition_query_request", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    self.service.approve,
                    **self.args,
                    actor_id="reviewer",
                    parameters={"order_id": 1},
                )
                try:
                    self.assertTrue(started.wait(10))
                    self.other_service.explain(**self.args, parameters={"order_id": 2})
                finally:
                    release.set()
                with self.assertRaises(QueryExecutionStaleError):
                    pending.result(timeout=10)
        self.assertEqual(QueryRequestState.READY_FOR_PREVIEW, self.current().state)
        self.assertIsNone(self.current().approved_sql_hash)

    def test_two_inflight_explains_cannot_both_publish_the_same_revision(self) -> None:
        started, release = Event(), Event()
        original = self.executor.explain

        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if args[3]["order_id"] == 1:
                started.set()
                if not release.wait(10):
                    raise TimeoutError("EXPLAIN synchronization timed out")
            return result

        with patch.object(self.executor, "explain", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.service.explain, **self.args, parameters={"order_id": 1})
                try:
                    self.assertTrue(started.wait(10))
                    winner = self.other_service.explain(**self.args, parameters={"order_id": 2})
                finally:
                    release.set()
                with self.assertRaises(QueryExecutionStaleError):
                    pending.result(timeout=10)
        self.assertEqual(1, winner.revision)
        approved = self.service.approve(
            **self.args,
            actor_id="reviewer",
            parameters={"order_id": 2},
            expected_explain_revision=1,
        )
        self.assertEqual(1, approved.explain_revision)

    def test_client_must_approve_the_revision_it_reviewed(self) -> None:
        self.service.explain(**self.args, parameters={"order_id": 1})
        self.service.explain(**self.args, parameters={"order_id": 1})
        with self.assertRaises(QueryExecutionStaleError):
            self.service.approve(
                **self.args,
                actor_id="reviewer",
                parameters={"order_id": 1},
                expected_explain_revision=1,
            )
        approved = self.service.approve(
            **self.args,
            actor_id="reviewer",
            parameters={"order_id": 1},
            expected_explain_revision=2,
        )
        self.assertEqual(2, approved.approved_explain_revision)

    def test_cancellation_while_explaining_cannot_publish_a_revision(self) -> None:
        original = self.executor.explain

        def cancelling(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            self.other_service.cancel(**self.args)
            return result

        with patch.object(self.executor, "explain", side_effect=cancelling):
            with self.assertRaises(QueryExecutionStaleError):
                self.service.explain(**self.args, parameters={"order_id": 1})
        self.assertEqual(QueryRequestState.CANCELLED, self.current().state)
        self.assertEqual(0, self.current().explain_revision)
        events = self.repository.audit_events(self.args["tenant_id"])
        self.assertFalse(any(event.event_type == "query.explained" for event in events))

    def test_changed_sql_and_legacy_unbound_approvals_never_execute(self) -> None:
        self.service.explain(**self.args, parameters={"order_id": 1})
        self.service.approve(**self.args, actor_id="reviewer", parameters={"order_id": 1})
        with self.repository._connection:
            self.repository._connection.execute(
                "UPDATE query_requests SET normalized_sql = ? WHERE tenant_id = ? AND id = ?",
                (
                    "SELECT id FROM public.orders WHERE id > %(order_id)s LIMIT 2",
                    self.args["tenant_id"],
                    self.args["request_id"],
                ),
            )
        with self.assertRaises(QueryExecutionStaleError):
            self.service.execute(**self.args, parameters={"order_id": 1})
        with self.repository._connection:
            self.repository._connection.execute(
                "UPDATE query_requests SET approved_sql_hash = NULL WHERE tenant_id = ? AND id = ?",
                (self.args["tenant_id"], self.args["request_id"]),
            )
        with self.assertRaises(QueryExecutionStaleError):
            self.service.execute(**self.args, parameters={"order_id": 1})
        self.assertEqual([], self.executor.execute_calls)

    def test_revision_history_preserves_prior_bindings(self) -> None:
        self.service.explain(**self.args, parameters={"order_id": 1})
        first = self.current()
        self.service.explain(**self.args, parameters={"order_id": 2})
        rows = self.repository._connection.execute(
            "SELECT * FROM query_explain_revisions WHERE tenant_id = ? AND request_id = ? "
            "ORDER BY revision",
            (self.args["tenant_id"], self.args["request_id"]),
        ).fetchall()
        self.assertEqual([1, 2], [row["revision"] for row in rows])
        self.assertEqual(first.parameter_value_hash, rows[0]["parameter_value_hash"])
        self.assertNotEqual(rows[0]["parameter_value_hash"], rows[1]["parameter_value_hash"])
        self.assertEqual(rows[0]["sql_hash"], rows[1]["sql_hash"])
        for sql in (
            "UPDATE query_explain_revisions SET revision = 99 WHERE tenant_id = ?",
            "DELETE FROM query_explain_revisions WHERE tenant_id = ?",
        ):
            with self.assertRaisesRegex(Exception, "immutable"):
                with self.repository._connection:
                    self.repository._connection.execute(sql, (self.args["tenant_id"],))

    def test_policy_change_reopens_approval_before_sql_is_dispatched(self) -> None:
        self.service.explain(**self.args, parameters={"order_id": 1})
        self.service.approve(**self.args, actor_id="reviewer", parameters={"order_id": 1})
        self.other.upsert_execution_cost_policy(ExecutionCostPolicy(
            tenant_id=self.args["tenant_id"], data_source_id=self.args["data_source_id"],
            max_total_cost=1,
        ))
        with self.assertRaises(QueryExecutionStaleError):
            self.service.execute(**self.args, parameters={"order_id": 1})
        self.assertEqual(QueryRequestState.READY_FOR_PREVIEW, self.current().state)
        self.assertIsNone(self.current().approved_cost_policy_revision)
        self.assertIsNone(self.current().approved_by)
        self.assertEqual([], self.executor.execute_calls)
        with self.assertRaises(QueryExecutionPolicyBlockedError):
            self.service.approve(**self.args, actor_id="reviewer", parameters={"order_id": 1})
        policy = self.other.upsert_execution_cost_policy(ExecutionCostPolicy(
            tenant_id=self.args["tenant_id"], data_source_id=self.args["data_source_id"],
            max_total_cost=100,
        ))
        self.assertEqual(2, policy.revision)
        approved = self.service.approve(
            **self.args, actor_id="reviewer", parameters={"order_id": 1},
        )
        self.assertEqual(2, approved.approved_cost_policy_revision)
        self.service.execute(**self.args, parameters={"order_id": 1})
        self.assertEqual(1, len(self.executor.execute_calls))

    def test_policy_update_between_check_and_execution_transition_is_blocked(self) -> None:
        self.service.explain(**self.args, parameters={"order_id": 1})
        self.service.approve(**self.args, actor_id="reviewer", parameters={"order_id": 1})
        original = self.repository.transition_query_request

        def changed(*args: Any, **kwargs: Any) -> Any:
            if args[2] is QueryRequestState.EXECUTING:
                self.other.upsert_execution_cost_policy(ExecutionCostPolicy(
                    tenant_id=self.args["tenant_id"], data_source_id=self.args["data_source_id"],
                    max_total_cost=100,  # Even a permissive change needs a fresh approval.
                ))
            return original(*args, **kwargs)

        with patch.object(self.repository, "transition_query_request", side_effect=changed):
            with self.assertRaises(QueryExecutionStaleError):
                self.service.execute(**self.args, parameters={"order_id": 1})
        self.assertEqual([], self.executor.execute_calls)
        self.assertEqual(QueryRequestState.READY_FOR_PREVIEW, self.current().state)

    def test_policy_update_between_check_and_approval_transition_is_blocked(self) -> None:
        self.service.explain(**self.args, parameters={"order_id": 1})
        original = self.repository.transition_query_request

        def changed(*args: Any, **kwargs: Any) -> Any:
            self.other.upsert_execution_cost_policy(ExecutionCostPolicy(
                tenant_id=self.args["tenant_id"], data_source_id=self.args["data_source_id"],
                max_total_cost=1,
            ))
            return original(*args, **kwargs)

        with patch.object(self.repository, "transition_query_request", side_effect=changed):
            with self.assertRaises(QueryExecutionStaleError):
                self.service.approve(**self.args, actor_id="reviewer", parameters={"order_id": 1})
        self.assertEqual(QueryRequestState.READY_FOR_PREVIEW, self.current().state)
        self.assertIsNone(self.current().approved_by)
