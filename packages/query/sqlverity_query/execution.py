from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from packages.authorized_query.sqlverity_authorized_query import (
    AuthorizedQueryCompiler,
    AuthorizedQueryMaterializationError,
)
from packages.domain.sqlverity_domain.contracts import (
    ExplainResult,
    PolicyEngine,
    QueryRequestStore,
    ReadOnlyExecutor,
    ReadOnlyResult,
    SQLProposal,
    SQLValidator,
)
from packages.domain.sqlverity_domain.models import (
    AuthorizedQueryDefinition,
    CatalogVersion,
    Classification,
    ColumnDefinition,
    DataSource,
    DataSourceCapability,
    DataSourceType,
    ExecutionCostPolicy,
    LLMUsageEvent,
    QueryRequest,
    QueryRequestState,
    SchemaObject,
)
from packages.result_engine.sqlverity_result_engine import (
    DeterministicAnswer,
    DeterministicResultProcessor,
    ResultPrivacyReport,
    ResultProvenance,
)

from .parameters import QueryParameterBindingError, bind_query_parameters


class QueryExecutionError(RuntimeError):
    pass


class QueryExecutionNotFoundError(QueryExecutionError):
    pass


class QueryExecutionStateError(QueryExecutionError):
    pass


class QueryExecutionStaleError(QueryExecutionError):
    pass


class QueryExecutionPolicyBlockedError(QueryExecutionError):
    pass


class QueryExecutionValidationError(QueryExecutionError):
    pass


class QueryExecutionUnavailableError(QueryExecutionError):
    pass


class QueryExecutionCatalog(QueryRequestStore, Protocol):
    def get_data_source(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> DataSource | None: ...

    def get_latest_catalog_version(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> CatalogVersion | None: ...

    def list_schema_objects(
        self,
        tenant_id: str,
        catalog_version_id: str,
    ) -> tuple[SchemaObject, ...]: ...

    def list_columns_for_catalog_version(
        self,
        tenant_id: str,
        catalog_version_id: str,
    ) -> tuple[ColumnDefinition, ...]: ...

    def get_llm_usage_event(
        self,
        tenant_id: str,
        event_id: str,
    ) -> LLMUsageEvent | None: ...

    def get_execution_cost_policy(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> ExecutionCostPolicy | None: ...

    def get_authorized_query_definition(
        self,
        tenant_id: str,
        data_source_id: str,
        catalog_version_id: str,
    ) -> AuthorizedQueryDefinition | None: ...


@dataclass(frozen=True, slots=True)
class QueryExecutionRun:
    query_request: QueryRequest
    result: ReadOnlyResult
    answer: DeterministicAnswer
    privacy: ResultPrivacyReport
    provenance: ResultProvenance


@dataclass(frozen=True, slots=True)
class _PreparedQuery:
    sql: str
    parameters: Mapping[str, object]
    parameter_names: tuple[str, ...] = ()
    parameter_value_hash: str | None = None


class QueryExecutionService:
    def __init__(
        self,
        catalog: QueryExecutionCatalog,
        validator: SQLValidator,
        policy_engine: PolicyEngine,
        executors: Mapping[str, ReadOnlyExecutor],
        result_processor: DeterministicResultProcessor,
        *,
        timeout_seconds: int = 15,
        max_rows: int = 500,
        max_result_bytes: int = 5_000_000,
        authorized_query_compiler: AuthorizedQueryCompiler | None = None,
    ) -> None:
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 1 and 300")
        if not 1 <= max_rows <= 10_000:
            raise ValueError("max_rows must be between 1 and 10000")
        if not 1_024 <= max_result_bytes <= 100_000_000:
            raise ValueError("max_result_bytes must be between 1024 and 100000000")
        self._catalog = catalog
        self._validator = validator
        self._policy_engine = policy_engine
        self._executors = {key.casefold(): value for key, value in executors.items()}
        self._result_processor = result_processor
        self._timeout_seconds = timeout_seconds
        self._max_rows = max_rows
        self._max_result_bytes = max_result_bytes
        self._authorized_query_compiler = (
            authorized_query_compiler or AuthorizedQueryCompiler()
        )

    def approve(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        request_id: str,
        actor_id: str,
        parameters: Mapping[str, object] | None = None,
    ) -> QueryRequest:
        query_request, data_source = self._load(tenant_id, data_source_id, request_id)
        if query_request.state is not QueryRequestState.READY_FOR_PREVIEW:
            raise QueryExecutionStateError(
                "Only a query ready for preview can be approved"
            )
        prepared = self._prepare(query_request, data_source, parameters)
        self._enforce_parameter_binding(query_request, data_source, prepared)
        self._enforce_execution_cost_policy(query_request)
        if DataSourceCapability.EXECUTE_READ_ONLY not in data_source.capabilities:
            raise QueryExecutionUnavailableError(
                "DataSource does not allow read-only execution"
            )
        if data_source.connection_secret_ref is None:
            raise QueryExecutionUnavailableError(
                "DataSource has no connection secret reference"
            )
        self._executor(data_source)
        return self._catalog.transition_query_request(
            tenant_id,
            request_id,
            QueryRequestState.APPROVED,
            actor_id=actor_id,
        )

    def explain(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        request_id: str,
        parameters: Mapping[str, object] | None = None,
    ) -> ExplainResult:
        query_request, data_source = self._load(tenant_id, data_source_id, request_id)
        if query_request.state not in {
            QueryRequestState.READY_FOR_PREVIEW,
            QueryRequestState.APPROVED,
        }:
            raise QueryExecutionStateError(
                "EXPLAIN requires a query ready for preview or already approved"
            )
        prepared = self._prepare(query_request, data_source, parameters)
        if query_request.state is QueryRequestState.APPROVED:
            self._enforce_parameter_binding(query_request, data_source, prepared)
        executor = self._executor(data_source)
        result = executor.explain(
            data_source,
            query_request.id,
            prepared.sql,
            prepared.parameters,
            timeout_seconds=self._timeout_seconds,
        )
        self._catalog.record_query_activity(
            tenant_id,
            request_id,
            "query.explained",
            {
                "estimated_total_cost": result.estimated_total_cost,
                "estimated_rows": result.estimated_rows,
                "elapsed_ms": result.elapsed_ms,
                "parameter_names": prepared.parameter_names,
                "parameter_value_hash": prepared.parameter_value_hash,
            },
        )
        return result

    def execute(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        request_id: str,
        parameters: Mapping[str, object] | None = None,
    ) -> QueryExecutionRun:
        query_request, data_source = self._load(tenant_id, data_source_id, request_id)
        if query_request.state is not QueryRequestState.APPROVED:
            raise QueryExecutionStateError("Read-only execution requires explicit approval")
        prepared = self._prepare(query_request, data_source, parameters)
        self._enforce_parameter_binding(query_request, data_source, prepared)
        executor = self._executor(data_source)
        executing = self._catalog.transition_query_request(
            tenant_id,
            request_id,
            QueryRequestState.EXECUTING,
        )
        try:
            result = executor.execute_read_only(
                data_source,
                request_id,
                prepared.sql,
                prepared.parameters,
                timeout_seconds=self._timeout_seconds,
                max_rows=self._max_rows,
                max_result_bytes=self._max_result_bytes,
            )
        except Exception:
            latest = self._catalog.get_query_request(tenant_id, request_id)
            if latest is not None and latest.state is QueryRequestState.EXECUTING:
                self._catalog.transition_query_request(
                    tenant_id,
                    request_id,
                    QueryRequestState.FAILED_EXECUTION,
                )
            raise
        succeeded = self._catalog.transition_query_request(
            tenant_id,
            executing.id,
            QueryRequestState.SUCCEEDED,
        )
        processing = self._catalog.transition_query_request(
            tenant_id,
            succeeded.id,
            QueryRequestState.RESULT_PROCESSING,
        )
        try:
            processed = self._result_processor.process(
                query_request=processing,
                data_source=data_source,
                result=result,
                column_classifications=self._column_classifications(processing),
                usage=self._usage(processing),
            )
            self._catalog.record_query_activity(
                tenant_id,
                request_id,
                "query.result_metadata",
                {
                    "row_count": result.row_count,
                    "result_bytes": result.result_bytes,
                    "elapsed_ms": result.elapsed_ms,
                    "truncated": result.truncated,
                    "truncation_reason": result.truncation_reason,
                    "processing_mode": processed.privacy.processing_mode,
                    "maximum_classification": (
                        processed.privacy.maximum_classification.value
                    ),
                    "masked_column_count": len(
                        processed.privacy.masked_output_columns
                    ),
                    "raw_rows_sent_to_llm": processed.privacy.raw_rows_sent_to_llm,
                },
            )
        except Exception:
            self._catalog.transition_query_request(
                tenant_id,
                processing.id,
                QueryRequestState.FAILED_EXECUTION,
            )
            raise
        completed = self._catalog.transition_query_request(
            tenant_id,
            processing.id,
            QueryRequestState.COMPLETED,
        )
        return QueryExecutionRun(
            query_request=completed,
            result=processed.result,
            answer=processed.answer,
            privacy=processed.privacy,
            provenance=processed.provenance,
        )

    def cancel(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        request_id: str,
    ) -> QueryRequest:
        query_request, data_source = self._load(tenant_id, data_source_id, request_id)
        if query_request.state not in {
            QueryRequestState.READY_FOR_PREVIEW,
            QueryRequestState.APPROVED,
            QueryRequestState.EXECUTING,
        }:
            raise QueryExecutionStateError("Query request cannot be cancelled in its current state")
        if query_request.state is QueryRequestState.EXECUTING:
            if DataSourceCapability.CANCEL not in data_source.capabilities:
                raise QueryExecutionUnavailableError("DataSource does not support cancellation")
            if not self._executor(data_source).cancel(request_id):
                raise QueryExecutionUnavailableError("Active database query could not be cancelled")
        return self._catalog.transition_query_request(
            tenant_id,
            request_id,
            QueryRequestState.CANCELLED,
        )

    def _load(
        self,
        tenant_id: str,
        data_source_id: str,
        request_id: str,
    ) -> tuple[QueryRequest, DataSource]:
        query_request = self._catalog.get_query_request(tenant_id, request_id)
        if query_request is None or query_request.data_source_id != data_source_id:
            raise QueryExecutionNotFoundError("Query request not found")
        data_source = self._catalog.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise QueryExecutionNotFoundError("DataSource not found")
        return query_request, data_source

    def _prepare(
        self,
        query_request: QueryRequest,
        data_source: DataSource,
        parameters: Mapping[str, object] | None,
    ) -> _PreparedQuery:
        self._require_current_catalog(query_request)
        sql = self._revalidate(query_request, data_source.dialect)
        classified_columns = self._column_classifications(query_request)
        missing_classifications = tuple(
            sorted(set(query_request.referenced_columns) - set(classified_columns))
        )
        if missing_classifications:
            raise QueryExecutionValidationError(
                "Stored SQL references columns missing from the catalog classification snapshot"
            )
        usage = self._usage(query_request)
        if query_request.llm_usage_event_id is not None and (
            usage is None
            or usage.provider_id != query_request.provider_id
            or usage.model_id != query_request.model_id
        ):
            raise QueryExecutionValidationError(
                "Stored query provenance does not match its LLM usage event"
            )
        decision = self._policy_engine.evaluate_sql_access(
            tenant_id=query_request.tenant_id,
            data_source_id=query_request.data_source_id,
            tables=query_request.referenced_tables,
            columns=query_request.referenced_columns,
        )
        if not decision.allowed:
            reasons = "; ".join(decision.reasons) or "SQL access denied"
            raise QueryExecutionPolicyBlockedError(reasons)
        supplied_parameters = parameters or {}
        if data_source.source_type is DataSourceType.AUTHORIZED_QUERY:
            definition = self._catalog.get_authorized_query_definition(
                query_request.tenant_id,
                query_request.data_source_id,
                query_request.catalog_version_id,
            )
            if definition is None:
                raise QueryExecutionValidationError(
                    "Authorized query definition is missing for the catalog version"
                )
            try:
                prepared = self._authorized_query_compiler.materialize(
                    definition=definition,
                    outer_sql=sql,
                    parameters=supplied_parameters,
                )
            except AuthorizedQueryMaterializationError as error:
                raise QueryExecutionValidationError(str(error)) from error
            return _PreparedQuery(
                sql=prepared.sql,
                parameters=prepared.parameters,
                parameter_names=prepared.parameter_names,
                parameter_value_hash=prepared.parameter_value_hash,
            )
        if query_request.parameter_definitions:
            try:
                bound = bind_query_parameters(
                    query_request.parameter_definitions,
                    supplied_parameters,
                )
            except QueryParameterBindingError as error:
                raise QueryExecutionValidationError(str(error)) from error
            return _PreparedQuery(
                sql=sql,
                parameters=bound.values,
                parameter_names=bound.names,
                parameter_value_hash=bound.value_hash,
            )
        if supplied_parameters:
            raise QueryExecutionValidationError(
                "This generated query does not declare execution parameters"
            )
        return _PreparedQuery(sql=sql, parameters={})

    def _require_current_catalog(self, query_request: QueryRequest) -> None:
        latest = self._catalog.get_latest_catalog_version(
            query_request.tenant_id,
            query_request.data_source_id,
        )
        if latest is None or latest.id != query_request.catalog_version_id:
            raise QueryExecutionStaleError(
                "Catalog changed after SQL validation; regenerate the query"
            )

    def _revalidate(self, query_request: QueryRequest, dialect: str) -> str:
        if query_request.normalized_sql is None:
            raise QueryExecutionValidationError("Query request has no validated SQL")
        result = self._validator.validate(
            SQLProposal(
                intent="data_query",
                sql=query_request.normalized_sql,
                dialect=dialect,
                tables=query_request.referenced_tables,
                columns=query_request.referenced_columns,
                parameters=query_request.parameter_definitions,
            ),
            allowed_tables=frozenset(query_request.referenced_tables),
            allowed_columns=frozenset(query_request.referenced_columns),
            max_rows=self._max_rows,
        )
        if not result.accepted or result.normalized_sql is None:
            raise QueryExecutionValidationError(
                "Stored SQL no longer passes the safety validator"
            )
        if (
            result.referenced_tables != tuple(sorted(query_request.referenced_tables))
            or result.referenced_columns != tuple(sorted(query_request.referenced_columns))
        ):
            raise QueryExecutionValidationError("Stored SQL reference lineage changed")
        return result.normalized_sql

    def _executor(self, data_source: DataSource) -> ReadOnlyExecutor:
        executor = self._executors.get(data_source.dialect.casefold())
        if executor is None:
            raise QueryExecutionUnavailableError(
                f"No read-only executor is configured for dialect {data_source.dialect}"
            )
        return executor

    def _column_classifications(
        self,
        query_request: QueryRequest,
    ) -> Mapping[str, Classification]:
        classifications: dict[str, Classification] = {}
        referenced = frozenset(query_request.referenced_columns)
        schema_objects = self._catalog.list_schema_objects(
            query_request.tenant_id,
            query_request.catalog_version_id,
        )
        objects_by_id = {schema_object.id: schema_object for schema_object in schema_objects}
        for column in self._catalog.list_columns_for_catalog_version(
            query_request.tenant_id,
            query_request.catalog_version_id,
        ):
            column_ref = f"{objects_by_id[column.schema_object_id].reference}.{column.name}"
            if column_ref in referenced:
                classifications[column_ref] = column.classification
        return classifications

    def _usage(self, query_request: QueryRequest) -> LLMUsageEvent | None:
        if query_request.llm_usage_event_id is None:
            return None
        return self._catalog.get_llm_usage_event(
            query_request.tenant_id,
            query_request.llm_usage_event_id,
        )

    def _enforce_execution_cost_policy(self, query_request: QueryRequest) -> None:
        policy = self._catalog.get_execution_cost_policy(
            query_request.tenant_id,
            query_request.data_source_id,
        )
        if policy is None:
            return
        if policy.require_explain and query_request.explained_at is None:
            raise QueryExecutionPolicyBlockedError(
                "Execution cost policy requires EXPLAIN before approval"
            )
        if policy.max_total_cost is not None:
            if query_request.estimated_db_cost is None:
                raise QueryExecutionPolicyBlockedError(
                    "Planner cost is unavailable for the configured threshold"
                )
            if query_request.estimated_db_cost > policy.max_total_cost:
                raise QueryExecutionPolicyBlockedError(
                    "Planner cost exceeds the DataSource execution threshold"
                )
        if policy.max_estimated_rows is not None:
            if query_request.estimated_db_rows is None:
                raise QueryExecutionPolicyBlockedError(
                    "Planner row estimate is unavailable for the configured threshold"
                )
            if query_request.estimated_db_rows > policy.max_estimated_rows:
                raise QueryExecutionPolicyBlockedError(
                    "Planner row estimate exceeds the DataSource execution threshold"
                )

    @staticmethod
    def _enforce_parameter_binding(
        query_request: QueryRequest,
        data_source: DataSource,
        prepared: _PreparedQuery,
    ) -> None:
        requires_binding = (
            data_source.source_type is DataSourceType.AUTHORIZED_QUERY
            or bool(prepared.parameter_names)
        )
        if not requires_binding:
            return
        if query_request.explained_at is None:
            raise QueryExecutionPolicyBlockedError(
                "Parameterized queries require EXPLAIN with bound values before approval"
            )
        if query_request.parameter_value_hash != prepared.parameter_value_hash:
            raise QueryExecutionPolicyBlockedError(
                "Query parameter bindings differ from the explained values"
            )
