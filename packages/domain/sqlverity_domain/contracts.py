from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from .models import (
    Classification,
    DataSource,
    DataSourceCapability,
    LLMUsageEvent,
    ObjectKind,
    OutputColumnLineage,
    QueryParameterDefinition,
    QueryRequest,
    QueryRequestState,
)


@dataclass(frozen=True, slots=True)
class ColumnSnapshot:
    name: str
    physical_type: str
    ordinal: int
    nullable: bool
    default_expression: str | None = None
    is_primary_key: bool = False
    comment: str | None = None
    classification: Classification = Classification.INTERNAL

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.physical_type.strip():
            raise ValueError("Column snapshot requires name and physical type")
        if self.ordinal < 1:
            raise ValueError("Column snapshot ordinal must be positive")


@dataclass(frozen=True, slots=True)
class SchemaObjectSnapshot:
    schema_name: str
    name: str
    kind: ObjectKind
    columns: tuple[ColumnSnapshot, ...]
    definition_sql: str | None = None
    comment: str | None = None

    @property
    def reference(self) -> str:
        return f"{self.schema_name}.{self.name}"

    def __post_init__(self) -> None:
        if not self.schema_name.strip() or not self.name.strip():
            raise ValueError("Schema object snapshot requires schema and name")


@dataclass(frozen=True, slots=True)
class RelationshipSnapshot:
    name: str
    source_object_ref: str
    target_object_ref: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.source_object_ref or not self.target_object_ref:
            raise ValueError("Relationship snapshot requires name and object references")
        if not self.source_columns or len(self.source_columns) != len(self.target_columns):
            raise ValueError("Relationship snapshot column lists must be non-empty and aligned")
        if any(not column.strip() for column in self.source_columns + self.target_columns):
            raise ValueError("Relationship snapshot columns must not be blank")


@dataclass(frozen=True, slots=True)
class DataSourceSnapshot:
    data_source_id: str
    dialect: str
    objects: tuple[SchemaObjectSnapshot, ...]
    relationships: tuple[RelationshipSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not self.data_source_id or not self.dialect.strip():
            raise ValueError("DataSource snapshot requires id and dialect")


@dataclass(frozen=True, slots=True)
class SQLProposal:
    intent: str
    sql: str
    dialect: str
    tables: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    business_concepts: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    business_rules: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    parameters: tuple[QueryParameterDefinition, ...] = ()
    ambiguities: tuple[str, ...] = ()
    needs_clarification: bool = False

    def __post_init__(self) -> None:
        if not self.intent.strip() or not self.dialect.strip():
            raise ValueError("SQL proposal requires intent and dialect")
        if not self.needs_clarification and not self.sql.strip():
            raise ValueError("SQL is required when clarification is not needed")


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class ValidationResult:
    dialect: str
    normalized_sql: str | None
    issues: tuple[ValidationIssue, ...] = ()
    referenced_tables: tuple[str, ...] = ()
    referenced_columns: tuple[str, ...] = ()
    output_lineage: tuple[OutputColumnLineage, ...] = ()
    output_lineage_complete: bool = False

    @property
    def accepted(self) -> bool:
        return not any(issue.blocking for issue in self.issues)


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    estimated_cost: str | None = None

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0 or self.cached_input_tokens < 0:
            raise ValueError("Token estimates must not be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens cannot exceed input tokens")


@dataclass(frozen=True, slots=True)
class LLMResponse:
    payload: Mapping[str, Any]
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("LLM response requires a model id")
        if (
            self.input_tokens < 0
            or self.output_tokens < 0
            or self.cached_input_tokens < 0
            or self.latency_ms < 0
        ):
            raise ValueError("LLM response counters must not be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens cannot exceed input tokens")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    redacted_fields: tuple[str, ...] = ()
    maximum_classification: Classification = Classification.INTERNAL
    metadata: Mapping[str, Any] = field(default_factory=dict)


class Connector(Protocol):
    def capabilities(self, data_source: DataSource) -> frozenset[DataSourceCapability]: ...

    def introspect(self, data_source: DataSource) -> DataSourceSnapshot: ...


class ReadOnlyExecutor(Protocol):

    def explain(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> ExplainResult: ...

    def execute_read_only(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
        max_rows: int,
        max_result_bytes: int,
    ) -> ReadOnlyResult: ...

    def cancel(self, request_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExplainResult:
    plan: Mapping[str, Any]
    estimated_total_cost: float | None
    estimated_rows: int | None
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class ReadOnlyResult:
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]
    row_count: int
    truncated: bool
    truncation_reason: str | None
    result_bytes: int
    elapsed_ms: int


class QueryRequestStore(Protocol):
    def create_query_request(self, query_request: QueryRequest) -> QueryRequest: ...

    def get_query_request(
        self,
        tenant_id: str,
        request_id: str,
    ) -> QueryRequest | None: ...

    def transition_query_request(
        self,
        tenant_id: str,
        request_id: str,
        target: QueryRequestState,
        *,
        actor_id: str | None = None,
    ) -> QueryRequest: ...

    def record_query_activity(
        self,
        tenant_id: str,
        request_id: str,
        event_type: str,
        details: Mapping[str, Any],
    ) -> None: ...


class LLMProvider(Protocol):
    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse: ...

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate: ...

    def capabilities(self) -> Mapping[str, Any]: ...

    def health_check(self) -> Mapping[str, Any]: ...


class LLMUsageRecorder(Protocol):
    def record_llm_usage(self, event: LLMUsageEvent) -> None: ...


class SQLValidator(Protocol):
    def validate(
        self,
        proposal: SQLProposal,
        *,
        allowed_tables: frozenset[str],
        allowed_columns: frozenset[str],
        max_rows: int,
    ) -> ValidationResult: ...


class PolicyEngine(Protocol):
    def evaluate_prompt_egress(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        content_manifest: Sequence[Mapping[str, Any]],
        data_source_id: str | None = None,
        purpose: str | None = None,
    ) -> PolicyDecision: ...

    def evaluate_sql_access(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        tables: Sequence[str],
        columns: Sequence[str],
    ) -> PolicyDecision: ...
