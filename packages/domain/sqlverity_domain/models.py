from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class DataSourceType(StrEnum):
    DIRECT_DB = "direct_db"
    LIMITED_SCHEMA = "limited_schema"
    VIEW_SOURCE = "view_source"
    AUTHORIZED_QUERY = "authorized_query"
    DDL_IMPORT = "ddl_import"
    MANUAL_SCHEMA = "manual_schema"
    METADATA_FILE = "metadata_file"
    HYBRID = "hybrid"


class DataSourceCapability(StrEnum):
    INTROSPECT = "introspect"
    PREVIEW = "preview"
    EXECUTE_READ_ONLY = "execute_read_only"
    EXPLAIN = "explain"
    CANCEL = "cancel"


class QueryParameterType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    UUID = "uuid"


class EpistemicStatus(StrEnum):
    CONFIRMED = "confirmed"
    IMPORTED = "imported"
    INFERRED = "inferred"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class AnalyticSemanticKind(StrEnum):
    METRIC = "metric"
    BUSINESS_RULE = "business_rule"


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"
    HIGHLY_SENSITIVE = "highly_sensitive"


class ObjectKind(StrEnum):
    SCHEMA = "schema"
    TABLE = "table"
    VIEW = "view"
    VIRTUAL_QUERY = "virtual_query"


class QueryRequestState(StrEnum):
    RECEIVED = "received"
    CONTEXT_BUILDING = "context_building"
    NEEDS_CLARIFICATION = "needs_clarification"
    LLM_GENERATING = "llm_generating"
    GENERATED = "generated"
    VALIDATING = "validating"
    REJECTED = "rejected"
    READY_FOR_PREVIEW = "ready_for_preview"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    RESULT_PROCESSING = "result_processing"
    COMPLETED = "completed"
    FAILED_LLM = "failed_llm"
    FAILED_VALIDATION = "failed_validation"
    FAILED_EXECUTION = "failed_execution"
    CANCELLED = "cancelled"
    POLICY_BLOCKED = "policy_blocked"


class QueryFeedbackOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CORRECTED = "corrected"


class GoldenCandidateStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlatformRole(StrEnum):
    ADMIN = "admin"
    DATA_STEWARD = "data_steward"
    ANALYST = "analyst"
    VIEWER = "viewer"


class BudgetPeriod(StrEnum):
    MONTHLY = "monthly"


class ProviderRetentionMode(StrEnum):
    ZERO = "zero"
    TEMPORARY = "temporary"
    PROVIDER_DEFAULT = "provider_default"
    LOCAL_RUNTIME = "local_runtime"


class ProviderDeploymentType(StrEnum):
    EXTERNAL_CLOUD = "external_cloud"
    LOCAL_PRIVATE = "local_private"


class BackgroundJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BackgroundJob:
    tenant_id: str
    job_type: str
    payload: Mapping[str, Any]
    data_source_id: str | None = None
    status: BackgroundJobStatus = BackgroundJobStatus.QUEUED
    attempt_count: int = 0
    max_attempts: int = 3
    scheduled_at: datetime = field(default_factory=utc_now)
    lease_expires_at: datetime | None = None
    worker_id: str | None = None
    result: Mapping[str, Any] | None = None
    last_error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,99}", self.job_type
        ):
            raise ValueError("Background job requires a tenant and safe job type")
        if self.data_source_id is not None and not self.data_source_id.strip():
            raise ValueError("Background job DataSource must not be blank")
        if self.attempt_count < 0 or not 1 <= self.max_attempts <= 10:
            raise ValueError("Background job retry counters are invalid")
        if self.worker_id is not None and not self.worker_id.strip():
            raise ValueError("Background job worker id must not be blank")
        if self.last_error_code is not None and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.]{0,199}", self.last_error_code
        ):
            raise ValueError("Background job error code is invalid")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        if self.result is not None:
            object.__setattr__(self, "result", MappingProxyType(dict(self.result)))


@dataclass(frozen=True, slots=True)
class AuthorizedQueryParameter:
    name: str
    physical_type: str
    nullable: bool = False

    def __post_init__(self) -> None:
        if not _valid_identifier(self.name):
            raise ValueError("Authorized query parameter name is not a safe identifier")
        if not self.physical_type.strip():
            raise ValueError("Authorized query parameter requires a physical type")


@dataclass(frozen=True, slots=True)
class AuthorizedQueryDefinition:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    version: int
    virtual_schema: str
    virtual_name: str
    description: str
    base_sql: str
    normalized_base_sql: str
    parameters: tuple[AuthorizedQueryParameter, ...] = ()
    allow_filtering: bool = True
    allow_aggregation: bool = True
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    @property
    def virtual_object_ref(self) -> str:
        return f"{self.virtual_schema}.{self.virtual_name}"

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.tenant_id,
                self.data_source_id,
                self.catalog_version_id,
                self.description,
                self.base_sql,
                self.normalized_base_sql,
            )
        ):
            raise ValueError("Authorized query definition has missing required fields")
        if self.version < 1:
            raise ValueError("Authorized query definition version must be positive")
        if not _valid_identifier(self.virtual_schema) or not _valid_identifier(
            self.virtual_name
        ):
            raise ValueError("Authorized query virtual schema and name must be safe identifiers")
        parameter_names = tuple(parameter.name.casefold() for parameter in self.parameters)
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("Authorized query parameter names must be unique")


@dataclass(frozen=True, slots=True)
class QueryParameterDefinition:
    name: str
    value_type: QueryParameterType
    nullable: bool = False

    def __post_init__(self) -> None:
        if not _valid_identifier(self.name):
            raise ValueError("Query parameter name is not a safe identifier")


@dataclass(frozen=True, slots=True)
class OutputColumnLineage:
    output_name: str
    source_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.output_name.strip():
            raise ValueError("Output lineage requires an output column name")
        if any(not value.strip() for value in self.source_columns):
            raise ValueError("Output lineage source columns must not be blank")
        if len(self.source_columns) != len(set(self.source_columns)):
            raise ValueError("Output lineage source columns must be unique")


@dataclass(frozen=True, slots=True)
class QueryRequest:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    sql_text: str
    normalized_sql: str | None
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    validation_issue_codes: tuple[str, ...]
    state: QueryRequestState
    business_concepts: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    business_rules: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    provider_id: str | None = None
    model_id: str | None = None
    llm_usage_event_id: str | None = None
    estimated_db_cost: float | None = None
    estimated_db_rows: int | None = None
    explained_at: datetime | None = None
    parameter_definitions: tuple[QueryParameterDefinition, ...] = ()
    parameter_names: tuple[str, ...] = ()
    parameter_value_hash: str | None = None
    output_lineage: tuple[OutputColumnLineage, ...] = ()
    output_lineage_complete: bool = False
    id: str = field(default_factory=new_id)
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.data_source_id or not self.catalog_version_id:
            raise ValueError("Query request requires tenant, DataSource, and catalog version")
        if self.state in {
            QueryRequestState.READY_FOR_PREVIEW,
            QueryRequestState.APPROVED,
            QueryRequestState.EXECUTING,
            QueryRequestState.SUCCEEDED,
            QueryRequestState.RESULT_PROCESSING,
            QueryRequestState.COMPLETED,
        } and not self.normalized_sql:
            raise ValueError("Executable query request requires normalized SQL")
        if self.approved_by is not None and not self.approved_by.strip():
            raise ValueError("Approval actor must not be blank")
        if (self.approved_by is None) != (self.approved_at is None):
            raise ValueError("Approval actor and timestamp must be recorded together")
        if (self.provider_id is None) != (self.model_id is None):
            raise ValueError("Query provider and model must be recorded together")
        if self.provider_id is not None and (
            not self.provider_id.strip() or not self.model_id or not self.model_id.strip()
        ):
            raise ValueError("Query provider and model must not be blank")
        if self.llm_usage_event_id is not None and (
            not self.llm_usage_event_id.strip() or self.provider_id is None
        ):
            raise ValueError("LLM usage link requires provider and model metadata")
        semantic_values = (
            self.business_concepts + self.metrics + self.business_rules + self.assumptions
        )
        if any(not value.strip() for value in semantic_values):
            raise ValueError("Query semantics and assumptions must not contain blanks")
        if self.estimated_db_cost is not None and self.estimated_db_cost < 0:
            raise ValueError("Estimated database cost must not be negative")
        if self.estimated_db_rows is not None and self.estimated_db_rows < 0:
            raise ValueError("Estimated database rows must not be negative")
        if any(not _valid_identifier(name) for name in self.parameter_names):
            raise ValueError("Query parameter names must be safe identifiers")
        if len(self.parameter_names) != len(
            {name.casefold() for name in self.parameter_names}
        ):
            raise ValueError("Query parameter names must be unique")
        definition_names = tuple(
            definition.name.casefold() for definition in self.parameter_definitions
        )
        if len(definition_names) != len(set(definition_names)):
            raise ValueError("Query parameter definitions must be unique")
        if self.parameter_definitions and (
            set(definition_names)
            != {name.casefold() for name in self.parameter_names}
        ):
            raise ValueError("Query parameter names must match their definitions")
        lineage_names = tuple(item.output_name.casefold() for item in self.output_lineage)
        if len(lineage_names) != len(set(lineage_names)):
            raise ValueError("Output lineage names must be unique")
        if self.parameter_value_hash is not None and (
            len(self.parameter_value_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.parameter_value_hash)
        ):
            raise ValueError("Query parameter hash must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class Tenant:
    name: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Tenant name must not be empty")


@dataclass(frozen=True, slots=True)
class DataSource:
    tenant_id: str
    name: str
    source_type: DataSourceType
    dialect: str
    capabilities: frozenset[DataSourceCapability] = field(default_factory=frozenset)
    connection_secret_ref: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.name.strip() or not self.dialect.strip():
            raise ValueError("DataSource tenant, name, and dialect are required")
        if self.connection_secret_ref is not None and not self.connection_secret_ref.strip():
            raise ValueError("Connection secret reference must not be blank")
        if (
            self.source_type is DataSourceType.AUTHORIZED_QUERY
            and DataSourceCapability.INTROSPECT in self.capabilities
        ):
            raise ValueError("Authorized query DataSource cannot allow catalog introspection")


@dataclass(frozen=True, slots=True)
class CatalogVersion:
    tenant_id: str
    data_source_id: str
    version: int
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Catalog version must be positive")


@dataclass(frozen=True, slots=True)
class SchemaObject:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    schema_name: str
    name: str
    kind: ObjectKind
    definition_sql: str | None = None
    id: str = field(default_factory=new_id)

    @property
    def reference(self) -> str:
        return f"{self.schema_name}.{self.name}"


@dataclass(frozen=True, slots=True)
class ColumnDefinition:
    tenant_id: str
    schema_object_id: str
    name: str
    physical_type: str
    ordinal: int
    nullable: bool = True
    classification: Classification = Classification.INTERNAL
    default_expression: str | None = None
    is_primary_key: bool = False
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("Column ordinal must be positive")


@dataclass(frozen=True, slots=True)
class Relationship:
    tenant_id: str
    catalog_version_id: str
    source_object_id: str
    target_object_id: str
    name: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    status: EpistemicStatus
    source: str
    confidence: float
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.source_columns or not self.target_columns:
            raise ValueError("Relationship name and columns are required")
        if len(self.source_columns) != len(self.target_columns):
            raise ValueError("Foreign-key column lists must have the same length")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class SemanticDefinition:
    tenant_id: str
    catalog_version_id: str
    object_ref: str
    description: str
    status: EpistemicStatus
    source: str
    confidence: float
    actor_id: str | None = None
    reason: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.object_ref.strip() or not self.description.strip() or not self.source.strip():
            raise ValueError("Semantic definition requires object, description, and source")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Confidence must be between 0 and 1")
        if self.status is EpistemicStatus.CONFLICTING:
            raise ValueError("CONFLICTING is a resolution state, not source evidence")
        if self.actor_id is not None and not self.actor_id.strip():
            raise ValueError("Semantic definition actor must not be blank")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("Semantic definition reason must not be blank")


@dataclass(frozen=True, slots=True)
class SemanticResolution:
    tenant_id: str
    data_source_id: str
    object_ref: str
    description: str
    status: EpistemicStatus
    confidence: float
    selected_definition_id: str | None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class BusinessConceptDefinition:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    concept_key: str
    name: str
    description: str
    synonyms: tuple[str, ...]
    object_refs: tuple[str, ...]
    content_classification: Classification
    status: EpistemicStatus
    source: str
    confidence: float
    actor_id: str | None = None
    reason: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            self.data_source_id,
            self.catalog_version_id,
            self.concept_key,
            self.name,
            self.description,
            self.source,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Business concept definition has missing required fields")
        if not _valid_identifier(self.concept_key):
            raise ValueError("Business concept key must be a safe identifier")
        if len(self.name) > 300 or len(self.description) > 20_000:
            raise ValueError("Business concept text exceeds the allowed length")
        if not self.object_refs:
            raise ValueError("Business concept must reference at least one catalog object")
        if any(not value.strip() for value in self.synonyms + self.object_refs):
            raise ValueError("Business concept lists must not contain blanks")
        normalized_synonyms = tuple(value.strip().casefold() for value in self.synonyms)
        if len(normalized_synonyms) != len(set(normalized_synonyms)):
            raise ValueError("Business concept synonyms must be unique")
        if self.name.strip().casefold() in normalized_synonyms:
            raise ValueError("Business concept name must not be repeated as a synonym")
        if len(self.object_refs) != len(set(self.object_refs)):
            raise ValueError("Business concept object references must be unique")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Confidence must be between 0 and 1")
        if self.status is EpistemicStatus.CONFLICTING:
            raise ValueError("CONFLICTING is a resolution state, not source evidence")
        optional = (self.actor_id, self.reason)
        if any(value is not None and not value.strip() for value in optional):
            raise ValueError("Business concept optional values must not be blank")


@dataclass(frozen=True, slots=True)
class BusinessConceptResolution:
    tenant_id: str
    data_source_id: str
    concept_key: str
    name: str
    description: str
    synonyms: tuple[str, ...]
    object_refs: tuple[str, ...]
    content_classification: Classification
    status: EpistemicStatus
    confidence: float
    selected_definition_id: str | None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    metric_key: str
    name: str
    description: str
    expression_sql: str
    normalized_expression_sql: str
    object_refs: tuple[str, ...]
    grain_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    rule_keys: tuple[str, ...]
    content_classification: Classification
    status: EpistemicStatus
    source: str
    confidence: float
    actor_id: str | None = None
    reason: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _validate_analytic_definition(
            key=self.metric_key,
            name=self.name,
            description=self.description,
            sql=self.expression_sql,
            normalized_sql=self.normalized_expression_sql,
            object_refs=self.object_refs,
            concept_keys=self.concept_keys,
            status=self.status,
            source=self.source,
            confidence=self.confidence,
            actor_id=self.actor_id,
            reason=self.reason,
        )
        if any(value not in self.object_refs for value in self.dimension_refs):
            raise ValueError("Metric dimensions must be included in its object references")
        if not self.grain_refs or any(
            value not in self.object_refs for value in self.grain_refs
        ):
            raise ValueError("Metric grain must reference at least one physical column")
        if len(self.grain_refs) != len(set(self.grain_refs)):
            raise ValueError("Metric grain references must be unique")
        if len(self.dimension_refs) != len(set(self.dimension_refs)):
            raise ValueError("Metric dimensions must be unique")
        if any(not _valid_identifier(value) for value in self.rule_keys):
            raise ValueError("Metric rule keys must be safe identifiers")
        if len(self.rule_keys) != len(set(self.rule_keys)):
            raise ValueError("Metric rule keys must be unique")


@dataclass(frozen=True, slots=True)
class MetricResolution:
    tenant_id: str
    data_source_id: str
    metric_key: str
    name: str
    description: str
    normalized_expression_sql: str
    object_refs: tuple[str, ...]
    grain_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    rule_keys: tuple[str, ...]
    content_classification: Classification
    status: EpistemicStatus
    confidence: float
    selected_definition_id: str | None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class BusinessRuleDefinition:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    rule_key: str
    name: str
    description: str
    predicate_sql: str
    normalized_predicate_sql: str
    object_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    content_classification: Classification
    status: EpistemicStatus
    source: str
    confidence: float
    actor_id: str | None = None
    reason: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _validate_analytic_definition(
            key=self.rule_key,
            name=self.name,
            description=self.description,
            sql=self.predicate_sql,
            normalized_sql=self.normalized_predicate_sql,
            object_refs=self.object_refs,
            concept_keys=self.concept_keys,
            status=self.status,
            source=self.source,
            confidence=self.confidence,
            actor_id=self.actor_id,
            reason=self.reason,
        )


@dataclass(frozen=True, slots=True)
class BusinessRuleResolution:
    tenant_id: str
    data_source_id: str
    rule_key: str
    name: str
    description: str
    normalized_predicate_sql: str
    object_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    content_classification: Classification
    status: EpistemicStatus
    confidence: float
    selected_definition_id: str | None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class CorrectedSQLExample:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    question: str
    normalized_question: str
    sql_text: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    actor_id: str
    revision: int
    content_classification: Classification
    business_concepts: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    reason: str | None = None
    source_query_request_id: str | None = None
    supersedes_example_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            self.data_source_id,
            self.catalog_version_id,
            self.question,
            self.normalized_question,
            self.sql_text,
            self.normalized_sql,
            self.actor_id,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Corrected SQL example has missing required fields")
        if len(self.question) > 10_000:
            raise ValueError("Corrected SQL example question exceeds 10000 characters")
        if len(self.sql_text) > 200_000 or len(self.normalized_sql) > 200_000:
            raise ValueError("Corrected SQL example SQL exceeds 200000 characters")
        if not self.referenced_tables:
            raise ValueError("Corrected SQL example must reference at least one table")
        values = (
            self.referenced_tables,
            self.referenced_columns,
            self.business_concepts,
            self.assumptions,
        )
        if any(any(not item.strip() for item in items) for items in values):
            raise ValueError("Corrected SQL example lists must not contain blanks")
        if any(len(items) != len(set(items)) for items in values):
            raise ValueError("Corrected SQL example lists must not contain duplicates")
        if self.revision < 1:
            raise ValueError("Corrected SQL example revision must be positive")
        if (self.revision == 1) != (self.supersedes_example_id is None):
            raise ValueError("Only the first corrected SQL revision can omit a predecessor")
        optional = (self.reason, self.source_query_request_id, self.supersedes_example_id)
        if any(value is not None and not value.strip() for value in optional):
            raise ValueError("Corrected SQL example optional identifiers must not be blank")


@dataclass(frozen=True, slots=True)
class QueryFeedbackEvent:
    tenant_id: str
    data_source_id: str
    query_request_id: str
    outcome: QueryFeedbackOutcome
    actor_id: str
    reason: str | None = None
    corrected_sql_example_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            self.data_source_id,
            self.query_request_id,
            self.actor_id,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Query feedback has missing required fields")
        if (self.outcome is QueryFeedbackOutcome.CORRECTED) != (
            self.corrected_sql_example_id is not None
        ):
            raise ValueError("Only corrected feedback must link a corrected SQL example")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("Query feedback reason must not be blank")


@dataclass(frozen=True, slots=True)
class GoldenEvaluationCandidate:
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    corrected_sql_example_id: str
    source_query_request_id: str
    question: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    assumptions: tuple[str, ...]
    content_classification: Classification
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            self.data_source_id,
            self.catalog_version_id,
            self.corrected_sql_example_id,
            self.source_query_request_id,
            self.question,
            self.normalized_sql,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Golden evaluation candidate has missing required fields")
        if not self.referenced_tables:
            raise ValueError("Golden evaluation candidate requires physical table lineage")
        values = (
            self.referenced_tables,
            self.referenced_columns,
            self.business_concepts,
            self.assumptions,
        )
        if any(any(not item.strip() for item in items) for items in values):
            raise ValueError("Golden evaluation candidate lists must not contain blanks")
        if any(len(items) != len(set(items)) for items in values):
            raise ValueError("Golden evaluation candidate lists must not contain duplicates")


@dataclass(frozen=True, slots=True)
class GoldenCandidateReview:
    tenant_id: str
    candidate_id: str
    status: GoldenCandidateStatus
    actor_id: str
    reason: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.tenant_id, self.candidate_id, self.actor_id)
        ):
            raise ValueError("Golden candidate review has missing required fields")
        if self.status is GoldenCandidateStatus.PROPOSED:
            raise ValueError("A review decision must approve or reject the candidate")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("Golden candidate review reason must not be blank")


@dataclass(frozen=True, slots=True)
class SecurityPrincipal:
    tenant_id: str
    subject: str
    display_name: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.tenant_id, self.subject, self.display_name)
        ):
            raise ValueError("Security principal has missing required fields")


@dataclass(frozen=True, slots=True)
class APICredential:
    tenant_id: str
    principal_id: str
    label: str
    token_sha256: str
    expires_at: datetime | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.tenant_id, self.principal_id, self.label)
        ):
            raise ValueError("API credential has missing required fields")
        if len(self.token_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.token_sha256
        ):
            raise ValueError("API credential token hash must be a lowercase SHA-256 digest")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("API credential expiry must follow creation")


@dataclass(frozen=True, slots=True)
class TenantRoleAssignment:
    tenant_id: str
    principal_id: str
    role: PlatformRole
    created_by: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.tenant_id, self.principal_id, self.created_by)
        ):
            raise ValueError("Tenant role assignment has missing required fields")


@dataclass(frozen=True, slots=True)
class DataSourceRoleAssignment:
    tenant_id: str
    data_source_id: str
    principal_id: str
    role: PlatformRole
    created_by: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.tenant_id,
                self.data_source_id,
                self.principal_id,
                self.created_by,
            )
        ):
            raise ValueError("DataSource role assignment has missing required fields")


@dataclass(frozen=True, slots=True)
class APICredentialRevocation:
    tenant_id: str
    credential_id: str
    actor_id: str
    reason: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.tenant_id, self.credential_id, self.actor_id)
        ):
            raise ValueError("API credential revocation has missing required fields")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("API credential revocation reason must not be blank")


@dataclass(frozen=True, slots=True)
class LLMUsageEvent:
    tenant_id: str
    provider_id: str
    model_id: str
    purpose: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cached_input_tokens: int = 0
    estimated_cost: str | None = None
    actual_cost: str | None = None
    currency: str | None = None
    pricing_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.tenant_id, self.provider_id, self.model_id, self.purpose)
        ):
            raise ValueError("LLM usage requires tenant, provider, model, and purpose")
        counters = (
            self.estimated_input_tokens,
            self.estimated_output_tokens,
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.latency_ms,
        )
        if any(value < 0 for value in counters):
            raise ValueError("LLM usage counters must not be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens cannot exceed input tokens")
        if self.currency is not None and not _valid_currency(self.currency):
            raise ValueError("LLM usage currency must be a three-letter uppercase code")


@dataclass(frozen=True, slots=True)
class AIContentManifestCount:
    kind: str
    classification: Classification
    included_count: int
    redacted_count: int

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("AI content manifest count requires a content kind")
        if self.included_count < 0 or self.redacted_count < 0:
            raise ValueError("AI content manifest counts must not be negative")


@dataclass(frozen=True, slots=True)
class AITransferReceipt:
    tenant_id: str
    data_source_id: str
    actor_id: str
    provider_id: str
    model_id: str
    purpose: str
    privacy_mode: str
    policy_scope: str
    declared_classification: Classification
    detected_classification: Classification
    effective_classification: Classification
    maximum_allowed_classification: Classification
    detection_reason_codes: tuple[str, ...]
    content_counts: tuple[AIContentManifestCount, ...]
    preflight_digest: str
    confirmation_outcome: str
    provider_invoked: bool
    decision_code: str
    provider_policy_id: str | None = None
    provider_policy_version: str | None = None
    llm_usage_event_id: str | None = None
    query_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    estimated_cost: str | None = None
    actual_cost: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            self.data_source_id,
            self.actor_id,
            self.provider_id,
            self.model_id,
            self.purpose,
            self.privacy_mode,
            self.policy_scope,
            self.preflight_digest,
            self.confirmation_outcome,
            self.decision_code,
        )
        if any(not value.strip() for value in required):
            raise ValueError("AI transfer receipt has missing required metadata")
        if len(self.preflight_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.preflight_digest
        ):
            raise ValueError("AI transfer receipt requires a SHA-256 preflight digest")
        if any(not reason.strip() for reason in self.detection_reason_codes):
            raise ValueError("AI transfer receipt reason codes must not be blank")
        optional_ids = (
            self.provider_policy_id,
            self.provider_policy_version,
            self.llm_usage_event_id,
            self.query_request_id,
        )
        if any(value is not None and not value.strip() for value in optional_ids):
            raise ValueError("AI transfer receipt optional references must not be blank")
        usage_counters = (self.input_tokens, self.output_tokens, self.latency_ms)
        if any(value is not None and value < 0 for value in usage_counters):
            raise ValueError("AI transfer receipt usage counters must not be negative")
        if self.provider_invoked and any(value is None for value in usage_counters):
            raise ValueError("Invoked AI transfer receipt requires token and latency telemetry")
        if not self.provider_invoked and any(value is not None for value in usage_counters):
            raise ValueError("Non-invoked AI transfer receipt must not report provider usage")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    tenant_id: str
    provider_id: str
    model_id: str
    valid_from: datetime
    currency: str
    token_unit: int
    input_price_per_unit: Decimal
    output_price_per_unit: Decimal
    source_version: str
    cached_input_price_per_unit: Decimal | None = None
    batch_discount: Decimal = Decimal("0")
    valid_to: datetime | None = None
    notes: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.tenant_id,
                self.provider_id,
                self.model_id,
                self.source_version,
            )
        ):
            raise ValueError("Model pricing requires tenant, provider, model, and source version")
        if not _valid_currency(self.currency):
            raise ValueError("Pricing currency must be a three-letter uppercase code")
        if not _timezone_aware(self.valid_from) or (
            self.valid_to is not None and not _timezone_aware(self.valid_to)
        ):
            raise ValueError("Pricing validity timestamps must be timezone-aware")
        if self.token_unit < 1:
            raise ValueError("Pricing token unit must be positive")
        prices = (
            self.input_price_per_unit,
            self.output_price_per_unit,
            self.cached_input_price_per_unit,
        )
        if any(price is not None and price < 0 for price in prices):
            raise ValueError("Model prices must not be negative")
        if not Decimal("0") <= self.batch_discount < Decimal("1"):
            raise ValueError("Batch discount must be between 0 and 1")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("Pricing valid_to must follow valid_from")
        if self.notes is not None and not self.notes.strip():
            raise ValueError("Pricing notes must not be blank")


@dataclass(frozen=True, slots=True)
class TenantBudget:
    tenant_id: str
    currency: str
    amount: Decimal
    valid_from: datetime
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    valid_to: datetime | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("Tenant budget requires tenant")
        if not _valid_currency(self.currency):
            raise ValueError("Budget currency must be a three-letter uppercase code")
        if not _timezone_aware(self.valid_from) or (
            self.valid_to is not None and not _timezone_aware(self.valid_to)
        ):
            raise ValueError("Budget validity timestamps must be timezone-aware")
        if self.amount <= 0:
            raise ValueError("Budget amount must be positive")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("Budget valid_to must follow valid_from")


@dataclass(frozen=True, slots=True)
class ExecutionCostPolicy:
    tenant_id: str
    data_source_id: str
    max_total_cost: float | None = None
    max_estimated_rows: int | None = None
    require_explain: bool = True
    id: str = field(default_factory=new_id)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.data_source_id:
            raise ValueError("Execution cost policy requires tenant and DataSource")
        if self.max_total_cost is None and self.max_estimated_rows is None:
            raise ValueError("Execution cost policy requires at least one threshold")
        if self.max_total_cost is not None and self.max_total_cost <= 0:
            raise ValueError("Maximum planner cost must be positive")
        if self.max_estimated_rows is not None and self.max_estimated_rows < 1:
            raise ValueError("Maximum estimated rows must be positive")


@dataclass(frozen=True, slots=True)
class ProviderEgressPolicy:
    tenant_id: str
    provider_id: str
    allowed_purposes: tuple[str, ...]
    maximum_classification: Classification
    data_residency: str
    retention_mode: ProviderRetentionMode
    data_source_id: str | None = None
    allowed: bool = True
    acknowledgement_digest: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    id: str = field(default_factory=new_id)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.provider_id.strip():
            raise ValueError("Provider policy requires tenant and provider")
        if self.data_source_id is not None and not self.data_source_id.strip():
            raise ValueError("Provider policy DataSource must not be blank")
        if not self.allowed_purposes or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", purpose)
            for purpose in self.allowed_purposes
        ):
            raise ValueError("Provider policy requires safe allowed purposes")
        if len(self.allowed_purposes) != len(set(self.allowed_purposes)):
            raise ValueError("Provider policy purposes must be unique")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", self.data_residency):
            raise ValueError("Provider policy data residency is invalid")
        acknowledgement_values = (
            self.acknowledgement_digest,
            self.acknowledged_by,
            self.acknowledged_at,
        )
        if any(value is None for value in acknowledgement_values) and any(
            value is not None for value in acknowledgement_values
        ):
            raise ValueError("Provider policy acknowledgement must be recorded atomically")
        if self.acknowledgement_digest is not None and (
            len(self.acknowledgement_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.acknowledgement_digest
            )
        ):
            raise ValueError("Provider policy acknowledgement digest must be SHA-256")
        if self.acknowledged_by is not None and not self.acknowledged_by.strip():
            raise ValueError("Provider policy acknowledgement actor must not be blank")

    @property
    def review_required(self) -> bool:
        return self.allowed and self.acknowledgement_digest is None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    tenant_id: str
    event_type: str
    subject_type: str
    subject_id: str
    details: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


def _valid_currency(value: str) -> bool:
    return len(value) == 3 and value.isascii() and value.isalpha() and value.isupper()


def _timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _valid_identifier(value: str) -> bool:
    return (
        bool(value)
        and (value[0].isalpha() or value[0] == "_")
        and all(
            character.isascii() and (character.isalnum() or character == "_")
            for character in value
        )
        and len(value) <= 63
    )


def _validate_analytic_definition(
    *,
    key: str,
    name: str,
    description: str,
    sql: str,
    normalized_sql: str,
    object_refs: tuple[str, ...],
    concept_keys: tuple[str, ...],
    status: EpistemicStatus,
    source: str,
    confidence: float,
    actor_id: str | None,
    reason: str | None,
) -> None:
    if not _valid_identifier(key):
        raise ValueError("Analytic semantic key must be a safe identifier")
    if any(not value.strip() for value in (name, description, sql, normalized_sql, source)):
        raise ValueError("Analytic semantic definition has missing required fields")
    if len(name) > 300 or len(description) > 20_000 or len(sql) > 20_000:
        raise ValueError("Analytic semantic definition exceeds the allowed length")
    if not object_refs or any(not value.strip() for value in object_refs):
        raise ValueError("Analytic semantic definition requires physical object references")
    if len(object_refs) != len(set(object_refs)):
        raise ValueError("Analytic semantic object references must be unique")
    if any(not _valid_identifier(value) for value in concept_keys):
        raise ValueError("Analytic semantic concept keys must be safe identifiers")
    if len(concept_keys) != len(set(concept_keys)):
        raise ValueError("Analytic semantic concept keys must be unique")
    if status is EpistemicStatus.CONFLICTING:
        raise ValueError("CONFLICTING is a resolution state, not source evidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("Confidence must be between 0 and 1")
    if any(value is not None and not value.strip() for value in (actor_id, reason)):
        raise ValueError("Analytic semantic optional values must not be blank")
