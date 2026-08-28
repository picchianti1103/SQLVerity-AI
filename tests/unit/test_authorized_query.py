from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from packages.authorized_query.sqlverity_authorized_query import (
    AuthorizedQueryCompiler,
    AuthorizedQueryConfigurationError,
    AuthorizedQueryMaterializationError,
    AuthorizedQueryRegistration,
    AuthorizedQueryService,
)
from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    ExplainResult,
    ReadOnlyResult,
    SQLProposal,
)
from packages.domain.sqlverity_domain.models import (
    AuthorizedQueryParameter,
    Classification,
    DataSourceCapability,
    DataSourceType,
    ObjectKind,
    QueryRequest,
    QueryRequestState,
)
from packages.llm_gateway.sqlverity_llm_gateway import SchemaQuestionPolicyEngine
from packages.query.sqlverity_query import (
    QueryExecutionPolicyBlockedError,
    QueryExecutionService,
)
from packages.result_engine.sqlverity_result_engine import DeterministicResultProcessor
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator


class CapturingAuthorizedExecutor:
    def __init__(self) -> None:
        self.explain_sql: str | None = None
        self.explain_parameters: dict[str, Any] | None = None
        self.execution_sql: str | None = None
        self.execution_parameters: dict[str, Any] | None = None

    def explain(
        self,
        data_source: Any,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> ExplainResult:
        self.explain_sql = sql
        self.explain_parameters = dict(parameters)
        return ExplainResult(
            plan={"Plan": {"Node Type": "Aggregate"}},
            estimated_total_cost=12.5,
            estimated_rows=5,
            elapsed_ms=2,
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
        self.execution_sql = sql
        self.execution_parameters = dict(parameters)
        return ReadOnlyResult(
            columns=("category", "total"),
            rows=({"category": "books", "total": 120},),
            row_count=1,
            truncated=False,
            truncation_reason=None,
            result_bytes=32,
            elapsed_ms=3,
        )

    def cancel(self, request_id: str) -> bool:
        return True


class AuthorizedQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="External sales",
            source_type=DataSourceType.AUTHORIZED_QUERY,
            dialect="postgresql",
            capabilities={
                DataSourceCapability.EXPLAIN,
                DataSourceCapability.EXECUTE_READ_ONLY,
            },
            connection_secret_ref="vault://external-sales",
        )
        self.compiler = AuthorizedQueryCompiler()
        self.service = AuthorizedQueryService(
            self.repository,
            CatalogIngestionService(self.repository, {}),
            self.compiler,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_registration_versions_virtual_schema_and_semantics(self) -> None:
        first = self._register()
        second = self._register(description="Updated governed sales surface")

        self.assertEqual(1, first.definition.version)
        self.assertEqual(2, second.definition.version)
        self.assertNotEqual(
            first.definition.catalog_version_id,
            second.definition.catalog_version_id,
        )
        self.assertIn("%(start_date)s", first.definition.normalized_base_sql)
        definitions = self.repository.list_authorized_query_definitions(
            self.tenant.id,
            self.data_source.id,
        )
        self.assertEqual((2, 1), tuple(item.version for item in definitions))

        objects = self.repository.list_schema_objects(
            self.tenant.id,
            first.definition.catalog_version_id,
        )
        self.assertEqual(1, len(objects))
        self.assertEqual(ObjectKind.VIRTUAL_QUERY, objects[0].kind)
        self.assertEqual("authorized.external_sales", objects[0].reference)
        columns = self.repository.list_columns(self.tenant.id, objects[0].id)
        self.assertEqual(
            ("customer_key", "sale_date", "category", "net_amount"),
            tuple(column.name for column in columns),
        )
        history = self.repository.list_semantic_definitions(
            self.tenant.id,
            self.data_source.id,
            "authorized.external_sales",
        )
        self.assertEqual("authorized_query_definition", history[0].source)

    def test_base_query_validation_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            AuthorizedQueryConfigurationError,
            "must be one SELECT",
        ):
            self._register(base_sql="DELETE FROM reporting.external_sales_view")
        with self.assertRaisesRegex(
            AuthorizedQueryConfigurationError,
            "output columns must be explicit",
        ):
            self._register(
                base_sql=(
                    "SELECT * FROM reporting.external_sales_view "
                    "WHERE sale_date >= CAST(:start_date AS DATE)"
                )
            )
        with self.assertRaisesRegex(
            AuthorizedQueryConfigurationError,
            "parameters do not match",
        ):
            self._register(
                base_sql=(
                    "SELECT customer_key, sale_date, category, net_amount "
                    "FROM reporting.external_sales_view"
                )
            )
        with self.assertRaisesRegex(
            AuthorizedQueryConfigurationError,
            "projections must match",
        ):
            self._register(
                base_sql=(
                    "SELECT customer_key, sale_date, category, gross_amount "
                    "FROM reporting.external_sales_view "
                    "WHERE sale_date >= CAST(:start_date AS DATE)"
                )
            )
        with self.assertRaisesRegex(
            AuthorizedQueryConfigurationError,
            "unsupported in the scalar binder",
        ):
            self.compiler.validate_base_query(
                base_sql=(
                    "SELECT category FROM reporting.external_sales_view "
                    "WHERE customer_key = ANY(:customer_keys)"
                ),
                parameters=(
                    AuthorizedQueryParameter(
                        name="customer_keys",
                        physical_type="text[]",
                    ),
                ),
                output_columns=(
                    ColumnSnapshot(
                        name="category",
                        physical_type="text",
                        ordinal=1,
                        nullable=True,
                    ),
                ),
                dialect="postgresql",
            )

        self.assertIsNone(
            self.repository.get_latest_catalog_version(
                self.tenant.id,
                self.data_source.id,
            )
        )

    def test_ast_materialization_binds_values_without_interpolation(self) -> None:
        definition = self._register().definition

        prepared = self.compiler.materialize(
            definition=definition,
            outer_sql=(
                "SELECT category, SUM(net_amount) AS total "
                "FROM authorized.external_sales GROUP BY category LIMIT 50"
            ),
            parameters={"start_date": "2026-01-01"},
        )
        repeated = self.compiler.materialize(
            definition=definition,
            outer_sql=(
                "SELECT category, SUM(net_amount) AS total "
                "FROM authorized.external_sales GROUP BY category LIMIT 50"
            ),
            parameters={"start_date": "2026-01-01"},
        )

        self.assertIn("FROM (SELECT", prepared.sql)
        self.assertIn("%(start_date)s", prepared.sql)
        self.assertNotIn("2026-01-01", prepared.sql)
        self.assertEqual({"start_date": "2026-01-01"}, dict(prepared.parameters))
        self.assertEqual(prepared.parameter_value_hash, repeated.parameter_value_hash)

    def test_materialization_enforces_policy_and_exact_bindings(self) -> None:
        definition = self._register().definition
        no_filtering = replace(definition, allow_filtering=False)
        no_aggregation = replace(definition, allow_aggregation=False)

        with self.assertRaisesRegex(
            AuthorizedQueryMaterializationError,
            "Filtering is disabled",
        ):
            self.compiler.materialize(
                definition=no_filtering,
                outer_sql=(
                    "SELECT category FROM authorized.external_sales "
                    "WHERE category = 'books'"
                ),
                parameters={"start_date": "2026-01-01"},
            )
        with self.assertRaisesRegex(
            AuthorizedQueryMaterializationError,
            "Aggregation is disabled",
        ):
            self.compiler.materialize(
                definition=no_aggregation,
                outer_sql="SELECT COUNT(*) FROM authorized.external_sales",
                parameters={"start_date": "2026-01-01"},
            )
        with self.assertRaisesRegex(
            AuthorizedQueryMaterializationError,
            "parameter mismatch",
        ):
            self.compiler.materialize(
                definition=definition,
                outer_sql="SELECT category FROM authorized.external_sales",
                parameters={},
            )
        with self.assertRaisesRegex(
            AuthorizedQueryMaterializationError,
            "does not match date",
        ):
            self.compiler.materialize(
                definition=definition,
                outer_sql="SELECT category FROM authorized.external_sales",
                parameters={"start_date": 20260101},
            )
        with self.assertRaisesRegex(
            AuthorizedQueryMaterializationError,
            "exactly once",
        ):
            self.compiler.materialize(
                definition=definition,
                outer_sql=(
                    "SELECT left_source.category FROM authorized.external_sales left_source "
                    "JOIN authorized.external_sales right_source "
                    "ON left_source.customer_key = right_source.customer_key"
                ),
                parameters={"start_date": "2026-01-01"},
            )

    def test_execution_requires_same_bound_values_as_explain(self) -> None:
        definition = self._register().definition
        validator = PostgreSQLSQLValidator()
        proposal = SQLProposal(
            intent="data_query",
            sql=(
                "SELECT category, SUM(net_amount) AS total "
                "FROM authorized.external_sales GROUP BY category"
            ),
            dialect="postgresql",
            tables=("authorized.external_sales",),
            columns=(
                "authorized.external_sales.category",
                "authorized.external_sales.net_amount",
            ),
        )
        validation = validator.validate(
            proposal,
            allowed_tables=frozenset(proposal.tables),
            allowed_columns=frozenset(proposal.columns),
            max_rows=50,
        )
        assert validation.normalized_sql is not None
        request = self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                catalog_version_id=definition.catalog_version_id,
                sql_text=proposal.sql,
                normalized_sql=validation.normalized_sql,
                referenced_tables=validation.referenced_tables,
                referenced_columns=validation.referenced_columns,
                validation_issue_codes=tuple(issue.code for issue in validation.issues),
                state=QueryRequestState.READY_FOR_PREVIEW,
            )
        )
        executor = CapturingAuthorizedExecutor()
        execution = QueryExecutionService(
            self.repository,
            validator,
            SchemaQuestionPolicyEngine(),
            {"postgresql": executor},
            DeterministicResultProcessor(),
            max_rows=50,
        )
        bindings = {"start_date": "2026-01-01"}

        with self.assertRaisesRegex(QueryExecutionPolicyBlockedError, "require EXPLAIN"):
            execution.approve(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=request.id,
                actor_id="reviewer",
                parameters=bindings,
            )
        execution.explain(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=request.id,
            parameters=bindings,
        )
        explained = self.repository.get_query_request(self.tenant.id, request.id)
        assert explained is not None
        self.assertEqual(("start_date",), explained.parameter_names)
        self.assertIsNotNone(explained.parameter_value_hash)
        self.assertEqual(bindings, executor.explain_parameters)
        assert executor.explain_sql is not None
        self.assertNotIn(bindings["start_date"], executor.explain_sql)

        approved = execution.approve(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=request.id,
            actor_id="reviewer",
            parameters=bindings,
        )
        with self.assertRaisesRegex(QueryExecutionPolicyBlockedError, "differ"):
            execution.explain(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=approved.id,
                parameters={"start_date": "2026-02-01"},
            )
        with self.assertRaisesRegex(QueryExecutionPolicyBlockedError, "differ"):
            execution.execute(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                request_id=approved.id,
                parameters={"start_date": "2026-02-01"},
            )
        run = execution.execute(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            request_id=approved.id,
            parameters=bindings,
        )

        self.assertEqual(QueryRequestState.COMPLETED, run.query_request.state)
        self.assertEqual(bindings, executor.execution_parameters)
        self.assertEqual(("start_date",), run.provenance.parameter_names)
        explained_event = next(
            event
            for event in self.repository.audit_events(self.tenant.id)
            if event.event_type == "query.explained"
        )
        self.assertNotIn("parameter_value_hash", explained_event.details)
        self.assertNotIn(bindings["start_date"], repr(explained_event.details))

    def _register(
        self,
        *,
        description: str = "Governed external sales",
        base_sql: str | None = None,
    ) -> AuthorizedQueryRegistration:
        return self.service.register(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            virtual_schema="authorized",
            virtual_name="external_sales",
            description=description,
            base_sql=base_sql
            or (
                "SELECT customer_key, sale_date, category, net_amount "
                "FROM reporting.external_sales_view "
                "WHERE sale_date >= CAST(:start_date AS DATE)"
            ),
            parameters=(
                AuthorizedQueryParameter(
                    name="start_date",
                    physical_type="date",
                ),
            ),
            output_columns=(
                ColumnSnapshot(
                    name="customer_key",
                    physical_type="text",
                    ordinal=1,
                    nullable=False,
                    classification=Classification.CONFIDENTIAL,
                ),
                ColumnSnapshot(
                    name="sale_date",
                    physical_type="date",
                    ordinal=2,
                    nullable=False,
                ),
                ColumnSnapshot(
                    name="category",
                    physical_type="text",
                    ordinal=3,
                    nullable=False,
                ),
                ColumnSnapshot(
                    name="net_amount",
                    physical_type="numeric",
                    ordinal=4,
                    nullable=False,
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
