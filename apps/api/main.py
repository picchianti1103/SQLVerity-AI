from __future__ import annotations

import json
import os
import secrets
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from packages.authorized_query.sqlverity_authorized_query import (
    AuthorizedQueryConfigurationError,
    AuthorizedQueryDataSourceNotFoundError,
    AuthorizedQueryRegistration,
    AuthorizedQueryService,
)
from packages.catalog.sqlverity_catalog.analytics_semantics import (
    AnalyticSemanticConcurrencyError,
    AnalyticSemanticEvidenceEntry,
    AnalyticSemanticNameConflictError,
    AnalyticSemanticNotFoundError,
    AnalyticSemanticReferenceError,
    AnalyticSemanticReviewItem,
    AnalyticSemanticValidationError,
    AnalyticsSemanticsService,
)
from packages.catalog.sqlverity_catalog.business_concepts import (
    BusinessConceptConcurrencyError,
    BusinessConceptCorrectionResult,
    BusinessConceptEvidenceEntry,
    BusinessConceptNotFoundError,
    BusinessConceptObjectNotFoundError,
    BusinessConceptReviewItem,
    BusinessConceptService,
    BusinessTermConflictError,
    BusinessTermResolution,
)
from packages.catalog.sqlverity_catalog.config import load_catalog_repository_from_environment
from packages.catalog.sqlverity_catalog.explorer import (
    CatalogNotIngestedError,
    SchemaExplorerService,
    SchemaExplorerSnapshot,
)
from packages.catalog.sqlverity_catalog.governance import (
    SemanticConcurrencyError,
    SemanticCorrectionResult,
    SemanticDescriptionRequiredError,
    SemanticEvidenceEntry,
    SemanticGovernanceService,
    SemanticObjectNotFoundError,
    SemanticReviewItem,
)
from packages.catalog.sqlverity_catalog.inference import (
    InvalidSemanticInferenceOutputError,
    SemanticInferenceNoTargetsError,
    SemanticInferenceRun,
    SemanticInferenceService,
)
from packages.catalog.sqlverity_catalog.ingestion import (
    CatalogIngestionError,
    CatalogIngestionService,
    ConnectionTestReport,
    DataSourceNotFoundError,
    IngestionReport,
)
from packages.catalog.sqlverity_catalog.offline_import import (
    OfflineImportModeError,
    OfflineSchemaImportService,
)
from packages.catalog.sqlverity_catalog.repository import (
    AnalyticSemanticWriteResult,
    BusinessConceptWriteResult,
    SQLiteCatalogRepository,
)
from packages.connectors.sqlverity_connectors.connection import (
    ConnectorConfigurationError,
    ConnectorUnavailableError,
    SecretResolutionError,
    load_secret_resolver_from_environment,
)
from packages.connectors.sqlverity_connectors.ddl import DDLParseError
from packages.connectors.sqlverity_connectors.mysql import MySQLConnector
from packages.connectors.sqlverity_connectors.mysql_executor import MySQLReadOnlyExecutor
from packages.connectors.sqlverity_connectors.oracle import OracleConnector
from packages.connectors.sqlverity_connectors.oracle_executor import OracleReadOnlyExecutor
from packages.connectors.sqlverity_connectors.postgresql import PostgreSQLConnector
from packages.connectors.sqlverity_connectors.postgresql_executor import (
    PostgreSQLReadOnlyExecutor,
    ReadOnlyExecutionError,
    ReadOnlyExecutorConfigurationError,
    ReadOnlyExecutorUnavailableError,
)
from packages.connectors.sqlverity_connectors.sqlserver import SQLServerConnector
from packages.connectors.sqlverity_connectors.sqlserver_executor import SQLServerReadOnlyExecutor
from packages.cost_engine.sqlverity_cost_engine import FinOpsService, FinOpsSummary
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    ExplainResult,
    RelationshipSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.epistemic import ResolutionAction
from packages.domain.sqlverity_domain.models import (
    AITransferReceipt,
    AnalyticSemanticKind,
    APICredentialRevocation,
    AuditEvent,
    AuthorizedQueryDefinition,
    AuthorizedQueryParameter,
    BackgroundJob,
    BackgroundJobStatus,
    BusinessConceptResolution,
    BusinessRuleResolution,
    Classification,
    DataSource,
    DataSourceCapability,
    DataSourceType,
    EpistemicStatus,
    ExecutionCostPolicy,
    GoldenCandidateStatus,
    MetricResolution,
    ModelPricing,
    ObjectKind,
    PlatformRole,
    ProviderEgressPolicy,
    ProviderRetentionMode,
    QueryFeedbackEvent,
    QueryFeedbackOutcome,
    QueryRequest,
    QueryRequestState,
    Tenant,
    TenantBudget,
    utc_now,
)
from packages.domain.sqlverity_domain.query_state import InvalidStateTransition
from packages.jobs.sqlverity_jobs import DurableJobWorker, JobExecutionOutcome
from packages.learning.sqlverity_learning import (
    CorrectedSQLConcurrencyError,
    CorrectedSQLExampleEntry,
    CorrectedSQLSourceNotFoundError,
    CorrectedSQLValidationError,
    FeedbackConflictError,
    FeedbackLinkNotFoundError,
    FeedbackNotEligibleError,
    FeedbackSummary,
    GoldenCandidateConflictError,
    GoldenCandidateEligibilityError,
    GoldenCandidateEntry,
    GoldenCandidateExport,
    GoldenCandidateNotFoundError,
    LearningGovernanceService,
    LearningLoopService,
    SQLExampleMatch,
)
from packages.llm_gateway.sqlverity_llm_gateway import (
    LLMBudgetExceededError,
    LLMGateway,
    LLMProviderCallError,
    LLMProviderCapabilityError,
    LLMProviderNotFoundError,
    PromptEgressBlockedError,
    SchemaQuestionPolicyEngine,
    load_llm_providers_from_environment,
)
from packages.observability.sqlverity_observability import (
    OperationalMetrics,
    RequestObservation,
    RequestTracer,
    load_request_tracer_from_environment,
)
from packages.query.sqlverity_query import (
    CurrentIntentEntity,
    GenerationPrivacyMode,
    GenerationStrategy,
    IntentCorrectionInterpreterService,
    IntentCorrectionRun,
    IntentMemoryCorrectionResult,
    IntentMemoryQueryNotFoundError,
    IntentMemoryReferenceError,
    IntentMemoryService,
    IntentMemoryStaleCatalogError,
    IntentMemoryTermConflictError,
    InvalidIntentCorrectionOutputError,
    InvalidSQLProposalOutputError,
    PreflightConfirmationError,
    PreflightConfirmationManager,
    QueryExecutionNotFoundError,
    QueryExecutionPolicyBlockedError,
    QueryExecutionRun,
    QueryExecutionService,
    QueryExecutionStaleError,
    QueryExecutionStateError,
    QueryExecutionUnavailableError,
    QueryExecutionValidationError,
    SQLGenerationPreflight,
    SQLGenerationRun,
    SQLGenerationService,
    policy_acknowledgement_digest,
)
from packages.result_engine.sqlverity_result_engine import (
    DeterministicResultProcessor,
    ResultShape,
)
from packages.retrieval.sqlverity_retrieval import (
    ContextBuilderService,
    ContextNoMatchesError,
    SchemaContextSnapshot,
)
from packages.security.sqlverity_security import (
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
    CredentialNotFoundError,
    IssuedAccess,
    OIDCAuthenticationError,
    OIDCBrowserFlow,
    PrincipalAccess,
    RequestQuotaLease,
    RequestQuotaLimits,
    RequestQuotaManager,
    ScopeQuota,
    SecurityAccessConflictError,
    SecurityPermission,
    ServerSideTextClassifier,
    load_oidc_authenticator_from_environment,
    load_oidc_browser_flow_from_environment,
)
from packages.sql_engine.sqlverity_sql_engine import (
    DEFAULT_DIALECT_REGISTRY,
    SQLValidatorRegistry,
)


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class SystemCapabilitiesView(BaseModel):
    service_version: str
    catalog_backend: str
    supported_dialects: tuple[str, ...]
    configured_provider_ids: tuple[str, ...]


class SecurityPrincipalCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=500)
    role: PlatformRole
    credential_label: str = Field(min_length=1, max_length=300)
    data_source_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None


class FederatedPrincipalCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=500)
    role: PlatformRole
    data_source_ids: tuple[str, ...] = ()


class BrowserAuthConfigView(BaseModel):
    enabled: bool
    login_url: str | None = None


class BrowserSessionView(BaseModel):
    principal_id: str
    subject: str
    display_name: str
    tenant_id: str | None
    authentication_method: str
    mfa_verified: bool


class SecurityPrincipalView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    subject: str
    display_name: str
    created_at: datetime


class DataSourceRoleAssignmentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    data_source_id: str
    role: PlatformRole
    created_at: datetime


class PrincipalAccessView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    principal: SecurityPrincipalView
    tenant_roles: tuple[PlatformRole, ...]
    data_source_roles: tuple[DataSourceRoleAssignmentView, ...]
    credentials: tuple[CredentialMetadataView, ...]


class CredentialMetadataView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    expires_at: datetime | None
    created_at: datetime
    revoked: bool


class IssuedAccessView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    principal: SecurityPrincipalView
    credential_id: str
    api_key: str
    role: PlatformRole
    data_source_ids: tuple[str, ...]
    expires_at: datetime | None


class CredentialRevocationCreate(BaseModel):
    reason: str | None = Field(default=None, max_length=10_000)


class CredentialRevocationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    credential_id: str
    actor_id: str
    reason: str | None
    created_at: datetime


class AuditEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    event_type: str
    subject_type: str
    subject_id: str
    details: dict[str, Any]
    created_at: datetime


class ModelPricingCreate(BaseModel):
    provider_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=300)
    valid_from: datetime
    valid_to: datetime | None = None
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    token_unit: int = Field(default=1_000_000, ge=1)
    input_price_per_unit: Decimal = Field(ge=0)
    cached_input_price_per_unit: Decimal | None = Field(default=None, ge=0)
    output_price_per_unit: Decimal = Field(ge=0)
    batch_discount: Decimal = Field(default=Decimal("0"), ge=0, lt=1)
    notes: str | None = Field(default=None, max_length=2_000)
    source_version: str = Field(min_length=1, max_length=300)


class ModelPricingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider_id: str
    model_id: str
    valid_from: datetime
    valid_to: datetime | None
    currency: str
    token_unit: int
    input_price_per_unit: Decimal
    cached_input_price_per_unit: Decimal | None
    output_price_per_unit: Decimal
    batch_discount: Decimal
    notes: str | None
    source_version: str
    created_at: datetime


class TenantBudgetCreate(BaseModel):
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(gt=0)
    valid_from: datetime
    valid_to: datetime | None = None


class TenantBudgetView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    currency: str
    amount: Decimal
    period: str
    valid_from: datetime
    valid_to: datetime | None
    created_at: datetime


class UsageBreakdownView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider_id: str
    model_id: str
    purpose: str
    event_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost: Decimal


class FinOpsSummaryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    currency: str
    period_start: datetime
    period_end: datetime
    total_cost: Decimal
    budget_id: str | None
    budget_amount: Decimal | None
    remaining_amount: Decimal | None
    priced_event_count: int
    unpriced_event_count: int
    breakdown: tuple[UsageBreakdownView, ...]


class ExecutionCostPolicyUpsert(BaseModel):
    max_total_cost: float | None = Field(default=None, gt=0)
    max_estimated_rows: int | None = Field(default=None, ge=1)
    require_explain: bool = True


class ExecutionCostPolicyView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    data_source_id: str
    max_total_cost: float | None
    max_estimated_rows: int | None
    require_explain: bool
    updated_at: datetime


class ProviderEgressPolicyUpsert(BaseModel):
    allowed: bool = True
    maximum_classification: Classification = Classification.INTERNAL
    allowed_purposes: tuple[str, ...] = Field(min_length=1, max_length=20)
    data_residency: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    retention_mode: ProviderRetentionMode
    acknowledged: bool = False


class ProviderEgressPolicyView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    data_source_id: str | None
    provider_id: str
    allowed: bool
    maximum_classification: Classification
    allowed_purposes: tuple[str, ...]
    data_residency: str
    retention_mode: ProviderRetentionMode
    acknowledgement_digest: str | None
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    review_required: bool
    updated_at: datetime


class AuthorizedQueryParameterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    physical_type: str = Field(min_length=1, max_length=500)
    nullable: bool = False


class AuthorizedQueryParameterView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    physical_type: str
    nullable: bool


class AuthorizedQueryColumnCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    physical_type: str = Field(min_length=1, max_length=500)
    nullable: bool = True
    classification: Classification = Classification.INTERNAL
    description: str | None = Field(default=None, max_length=10_000)


class AuthorizedQueryDefinitionCreate(BaseModel):
    virtual_schema: str = Field(
        default="authorized",
        min_length=1,
        max_length=63,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    virtual_name: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    description: str = Field(min_length=1, max_length=20_000)
    base_sql: str = Field(min_length=1, max_length=200_000)
    parameters: tuple[AuthorizedQueryParameterCreate, ...] = ()
    output_columns: tuple[AuthorizedQueryColumnCreate, ...] = Field(min_length=1)
    allow_filtering: bool = True
    allow_aggregation: bool = True


class AuthorizedQueryDefinitionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    catalog_version_id: str
    version: int
    virtual_schema: str
    virtual_name: str
    virtual_object_ref: str
    description: str
    base_sql: str
    normalized_base_sql: str
    parameters: tuple[AuthorizedQueryParameterView, ...]
    allow_filtering: bool
    allow_aggregation: bool
    created_at: datetime


class AuthorizedQueryRegistrationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    definition: AuthorizedQueryDefinitionView
    ingestion: IngestionView


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: DataSourceType
    dialect: str = Field(default="postgresql", min_length=1, max_length=50)
    capabilities: frozenset[DataSourceCapability] = Field(default_factory=frozenset)
    connection_secret_ref: str | None = Field(default=None, min_length=1, max_length=500)


class DataSourceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    name: str
    source_type: DataSourceType
    dialect: str
    capabilities: frozenset[DataSourceCapability]


class IngestionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    catalog_version_id: str
    catalog_version: int
    object_count: int
    column_count: int
    relationship_count: int
    imported_description_count: int


class ConnectionTestView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    data_source_id: str
    dialect: str
    object_count: int
    relationship_count: int
    capabilities: tuple[str, ...]


class DDLImportCreate(BaseModel):
    ddl: str = Field(min_length=1, max_length=2_000_000)
    default_schema: str | None = Field(default=None, min_length=1, max_length=200)


class ManualColumnCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    physical_type: str = Field(min_length=1, max_length=500)
    ordinal: int = Field(ge=1)
    nullable: bool = True
    classification: Classification = Classification.INTERNAL
    default_expression: str | None = Field(default=None, max_length=2_000)
    is_primary_key: bool = False
    comment: str | None = Field(default=None, max_length=10_000)


class ManualObjectCreate(BaseModel):
    schema_name: str = Field(default="public", min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    kind: ObjectKind
    columns: tuple[ManualColumnCreate, ...] = Field(min_length=1)
    definition_sql: str | None = Field(default=None, max_length=100_000)
    comment: str | None = Field(default=None, max_length=10_000)


class ManualRelationshipCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    source_object_ref: str = Field(min_length=3, max_length=500)
    target_object_ref: str = Field(min_length=3, max_length=500)
    source_columns: tuple[str, ...] = Field(min_length=1)
    target_columns: tuple[str, ...] = Field(min_length=1)


class ManualImportCreate(BaseModel):
    objects: tuple[ManualObjectCreate, ...] = Field(min_length=1)
    relationships: tuple[ManualRelationshipCreate, ...] = ()


class SemanticExplorerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    description: str
    status: EpistemicStatus
    confidence: float
    updated_at: datetime


class ColumnExplorerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    physical_type: str
    ordinal: int
    nullable: bool
    classification: Classification
    default_expression: str | None
    is_primary_key: bool
    semantics: SemanticExplorerView | None


class ObjectExplorerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    reference: str
    schema_name: str
    name: str
    kind: ObjectKind
    definition_sql: str | None
    semantics: SemanticExplorerView | None
    columns: tuple[ColumnExplorerView, ...]


class RelationshipExplorerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    source_object_ref: str
    target_object_ref: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    status: EpistemicStatus
    confidence: float


class SchemaExplorerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    data_source_id: str
    catalog_version_id: str
    catalog_version: int
    created_at: datetime
    objects: tuple[ObjectExplorerView, ...]
    relationships: tuple[RelationshipExplorerView, ...]


class SemanticEvidenceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    catalog_version_id: str
    description: str
    status: EpistemicStatus
    source: str
    confidence: float
    actor_id: str | None
    reason: str | None
    created_at: datetime
    selected: bool


class SemanticReviewView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    object_ref: str
    description: str
    status: EpistemicStatus
    confidence: float
    updated_at: datetime
    evidence: tuple[SemanticEvidenceView, ...]


class SemanticCorrectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_ref: str = Field(min_length=3, max_length=700)
    description: str | None = Field(default=None, max_length=20_000)
    reason: str | None = Field(default=None, max_length=10_000)
    expected_updated_at: datetime | None = None


class SemanticDefinitionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    catalog_version_id: str
    object_ref: str
    description: str
    status: EpistemicStatus
    source: str
    confidence: float
    actor_id: str | None
    reason: str | None
    created_at: datetime


class SemanticResolutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    data_source_id: str
    object_ref: str
    description: str
    status: EpistemicStatus
    confidence: float
    selected_definition_id: str | None
    updated_at: datetime


class SemanticCorrectionResultView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    definition: SemanticDefinitionView
    resolution: SemanticResolutionView


class BusinessConceptProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept_key: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20_000)
    synonyms: tuple[str, ...] = ()
    object_refs: tuple[str, ...] = Field(min_length=1)
    content_classification: Classification
    status: EpistemicStatus
    source: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=10_000)


class BusinessConceptCorrectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept_key: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20_000)
    synonyms: tuple[str, ...] = ()
    object_refs: tuple[str, ...] = Field(min_length=1)
    content_classification: Classification
    reason: str | None = Field(default=None, max_length=10_000)
    expected_updated_at: datetime | None = None


class BusinessConceptDefinitionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
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
    actor_id: str | None
    reason: str | None
    created_at: datetime


class BusinessConceptResolutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    updated_at: datetime


class BusinessConceptWriteView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence: BusinessConceptDefinitionView
    resolution: BusinessConceptResolutionView
    action: ResolutionAction


class BusinessConceptCorrectionResultView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    definition: BusinessConceptDefinitionView
    resolution: BusinessConceptResolutionView


class BusinessConceptEvidenceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    definition: BusinessConceptDefinitionView
    selected: bool


class BusinessConceptReviewView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resolution: BusinessConceptResolutionView
    evidence: tuple[BusinessConceptEvidenceView, ...]


class BusinessTermResolutionCreate(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)


class BusinessConceptMatchView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resolution: BusinessConceptResolutionView
    matched_terms: tuple[str, ...]


class BusinessTermAmbiguityView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    term: str
    concept_keys: tuple[str, ...]


class BusinessTermResolutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    matches: tuple[BusinessConceptMatchView, ...]
    ambiguities: tuple[BusinessTermAmbiguityView, ...]


class MetricProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_key: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20_000)
    expression_sql: str = Field(min_length=1, max_length=20_000)
    grain_refs: tuple[str, ...] = Field(min_length=1)
    dimension_refs: tuple[str, ...] = ()
    concept_keys: tuple[str, ...] = ()
    rule_keys: tuple[str, ...] = ()
    content_classification: Classification
    status: EpistemicStatus
    source: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=10_000)


class MetricCorrectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_key: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20_000)
    expression_sql: str = Field(min_length=1, max_length=20_000)
    grain_refs: tuple[str, ...] = Field(min_length=1)
    dimension_refs: tuple[str, ...] = ()
    concept_keys: tuple[str, ...] = ()
    rule_keys: tuple[str, ...] = ()
    content_classification: Classification
    reason: str | None = Field(default=None, max_length=10_000)
    expected_updated_at: datetime | None = None


class BusinessRuleProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_key: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20_000)
    predicate_sql: str = Field(min_length=1, max_length=20_000)
    concept_keys: tuple[str, ...] = ()
    content_classification: Classification
    status: EpistemicStatus
    source: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=10_000)


class BusinessRuleCorrectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_key: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20_000)
    predicate_sql: str = Field(min_length=1, max_length=20_000)
    concept_keys: tuple[str, ...] = ()
    content_classification: Classification
    reason: str | None = Field(default=None, max_length=10_000)
    expected_updated_at: datetime | None = None


class MetricDefinitionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
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
    actor_id: str | None
    reason: str | None
    created_at: datetime


class MetricResolutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    updated_at: datetime


class BusinessRuleDefinitionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
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
    actor_id: str | None
    reason: str | None
    created_at: datetime


class BusinessRuleResolutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    updated_at: datetime


class MetricWriteView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence: MetricDefinitionView
    resolution: MetricResolutionView
    action: ResolutionAction


class BusinessRuleWriteView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence: BusinessRuleDefinitionView
    resolution: BusinessRuleResolutionView
    action: ResolutionAction


class AnalyticSemanticEvidenceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    definition: MetricDefinitionView | BusinessRuleDefinitionView
    selected: bool


class AnalyticSemanticReviewView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resolution: MetricResolutionView | BusinessRuleResolutionView
    evidence: tuple[AnalyticSemanticEvidenceView, ...]


class CorrectedSQLExampleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=10_000)
    corrected_sql: str = Field(min_length=1, max_length=200_000)
    content_classification: Classification
    business_concepts: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    reason: str | None = Field(default=None, max_length=10_000)
    source_query_request_id: str | None = Field(default=None, min_length=1)
    supersedes_example_id: str | None = Field(default=None, min_length=1)


class CorrectedSQLExampleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    data_source_id: str
    catalog_version_id: str
    question: str
    normalized_question: str
    content_classification: Classification
    sql_text: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    assumptions: tuple[str, ...]
    actor_id: str
    reason: str | None
    source_query_request_id: str | None
    supersedes_example_id: str | None
    revision: int
    created_at: datetime


class CorrectedSQLExampleEntryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    example: CorrectedSQLExampleView
    is_active: bool


class SQLExampleRetrievalCreate(BaseModel):
    question: str = Field(min_length=1, max_length=10_000)
    max_results: int = Field(default=3, ge=0, le=10)


class SQLExampleMatchView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    example: CorrectedSQLExampleView
    score: float


class QueryFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: QueryFeedbackOutcome
    reason: str | None = Field(default=None, max_length=10_000)
    corrected_sql_example_id: str | None = Field(default=None, min_length=1)


class QueryFeedbackView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    data_source_id: str
    query_request_id: str
    outcome: QueryFeedbackOutcome
    actor_id: str
    reason: str | None
    corrected_sql_example_id: str | None
    created_at: datetime


class FeedbackSummaryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_count: int
    accepted_count: int
    rejected_count: int
    corrected_count: int
    acceptance_rate: float | None
    correction_rate: float | None


class GoldenCandidateCreate(BaseModel):
    corrected_sql_example_id: str = Field(min_length=1)


class GoldenCandidateView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
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
    created_at: datetime


class GoldenCandidateReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: GoldenCandidateStatus
    reason: str | None = Field(default=None, max_length=10_000)


class GoldenCandidateReviewView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    candidate_id: str
    status: GoldenCandidateStatus
    actor_id: str
    reason: str | None
    created_at: datetime


class GoldenCandidateEntryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate: GoldenCandidateView
    status: GoldenCandidateStatus
    review: GoldenCandidateReviewView | None


class GoldenCandidateExportItemView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class GoldenCandidateExportView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    format_version: int
    candidates: tuple[GoldenCandidateExportItemView, ...]


class SemanticInferenceCreate(BaseModel):
    provider_id: str = Field(min_length=1, max_length=200)


class LLMUsageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider_id: str
    model_id: str
    purpose: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    latency_ms: int
    estimated_cost: str | None
    actual_cost: str | None
    currency: str | None
    pricing_id: str | None
    created_at: datetime


class SemanticInferenceWriteView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    definition: SemanticDefinitionView
    resolution: SemanticResolutionView
    action: ResolutionAction


class SemanticInferenceRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    catalog_version_id: str
    provider_id: str
    model_id: str
    proposals: tuple[SemanticInferenceWriteView, ...]
    usage: LLMUsageView
    redacted_object_refs: tuple[str, ...]
    remaining_target_count: int
    last_target_ref: str | None


class SemanticInferenceJobCreate(BaseModel):
    provider_id: str = Field(min_length=1, max_length=100)
    max_attempts: int = Field(default=3, ge=1, le=10)


class BackgroundJobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    data_source_id: str | None
    job_type: str
    status: BackgroundJobStatus
    attempt_count: int
    max_attempts: int
    scheduled_at: datetime
    lease_expires_at: datetime | None
    result: dict[str, Any] | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class ContextPreviewCreate(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    max_seed_objects: int = Field(default=5, ge=1, le=20)
    max_objects: int = Field(default=12, ge=1, le=50)
    graph_hops: int = Field(default=1, ge=0, le=3)
    target_columns_per_object: int = Field(default=20, ge=1, le=100)
    max_sql_examples: int = Field(default=3, ge=0, le=10)


class ContextSemanticView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    description: str
    status: EpistemicStatus
    confidence: float
    updated_at: datetime


class ContextColumnView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    physical_type: str
    nullable: bool
    classification: Classification
    is_primary_key: bool
    semantics: ContextSemanticView | None
    selection_reasons: tuple[str, ...]


class ContextSchemaObjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    reference: str
    kind: ObjectKind
    lexical_score: int
    graph_expanded: bool
    selection_reasons: tuple[str, ...]
    semantics: ContextSemanticView | None
    columns: tuple[ContextColumnView, ...]
    omitted_column_count: int


class ContextRelationshipView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    source_object_ref: str
    target_object_ref: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    status: EpistemicStatus
    confidence: float


class ContextSQLExampleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    catalog_version_id: str
    question: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    revision: int
    score: float
    classification: Classification


class ContextBusinessConceptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    concept_key: str
    name: str
    description: str
    synonyms: tuple[str, ...]
    object_refs: tuple[str, ...]
    matched_terms: tuple[str, ...]
    status: EpistemicStatus
    confidence: float
    classification: Classification


class ContextBusinessTermAmbiguityView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    term: str
    concept_keys: tuple[str, ...]


class ContextMetricView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    metric_key: str
    name: str
    description: str
    normalized_expression_sql: str
    object_refs: tuple[str, ...]
    grain_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    rule_keys: tuple[str, ...]
    matched_terms: tuple[str, ...]
    status: EpistemicStatus
    confidence: float
    classification: Classification


class ContextBusinessRuleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_key: str
    name: str
    description: str
    normalized_predicate_sql: str
    object_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    matched_terms: tuple[str, ...]
    selected_by_metrics: tuple[str, ...]
    status: EpistemicStatus
    confidence: float
    classification: Classification


class SchemaContextView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    data_source_id: str
    catalog_version_id: str
    catalog_version: int
    dialect: str
    query: str
    query_terms: tuple[str, ...]
    selection_strategy: str
    objects: tuple[ContextSchemaObjectView, ...]
    relationships: tuple[ContextRelationshipView, ...]
    matched_seed_count: int
    omitted_object_count: int
    sql_examples: tuple[ContextSQLExampleView, ...]
    business_concepts: tuple[ContextBusinessConceptView, ...]
    business_term_ambiguities: tuple[ContextBusinessTermAmbiguityView, ...]
    metrics: tuple[ContextMetricView, ...]
    business_rules: tuple[ContextBusinessRuleView, ...]


class SQLGenerationCreate(ContextPreviewCreate):
    provider_id: str = Field(min_length=1, max_length=200)
    question_classification: Classification
    privacy_mode: GenerationPrivacyMode = "maximum_privacy"
    force_semantic: bool = False
    confirmation_token: str | None = Field(default=None, min_length=20, max_length=20_000)


class SQLGenerationPreflightCreate(ContextPreviewCreate):
    provider_id: str = Field(min_length=1, max_length=200)
    question_classification: Classification
    privacy_mode: GenerationPrivacyMode = "maximum_privacy"
    force_semantic: bool = False


class AIContentManifestCountView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    classification: Classification
    included_count: int
    redacted_count: int


class AITransferReceiptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    data_source_id: str
    actor_id: str
    provider_id: str
    model_id: str
    purpose: str
    privacy_mode: str
    provider_policy_id: str | None
    policy_scope: str
    provider_policy_version: str | None
    declared_classification: Classification
    detected_classification: Classification
    effective_classification: Classification
    maximum_allowed_classification: Classification
    detection_reason_codes: tuple[str, ...]
    content_counts: tuple[AIContentManifestCountView, ...]
    preflight_digest: str
    confirmation_outcome: str
    provider_invoked: bool
    decision_code: str
    llm_usage_event_id: str | None
    query_request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    estimated_cost: str | None
    actual_cost: str | None
    created_at: datetime


class SQLGenerationPreflightView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider_id: str
    model_id: str
    purpose: str
    data_source_id: str
    catalog_version_id: str
    policy_id: str | None
    policy_scope: str
    policy_version: str | None
    maximum_allowed_classification: Classification
    declared_classification: Classification
    detected_classification: Classification
    effective_classification: Classification
    detection_reason_codes: tuple[str, ...]
    data_residency: str
    retention_mode: str
    deployment_type: str
    allowed: bool
    decision_code: str
    review_required: bool
    content_counts: tuple[AIContentManifestCountView, ...]
    included_content_ids: tuple[str, ...]
    redacted_content_ids: tuple[str, ...]
    semantic_retry_possible: bool
    maximum_provider_calls: int
    provider_invoked: bool
    manifest_digest: str
    question_digest: str
    confirmation_token: str | None
    confirmation_expires_at: datetime | None
    receipt_id: str | None


class ProviderDeploymentView(BaseModel):
    provider_id: str
    model_id: str
    deployment_type: str
    data_residency: str
    retention_mode: ProviderRetentionMode


class EffectiveProviderPrivacyView(BaseModel):
    deployment: ProviderDeploymentView
    policy: ProviderEgressPolicyView | None
    policy_scope: str
    review_required: bool
    deployment_matches_policy: bool
    decision_code: str


class QueryParameterDefinitionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    value_type: str
    nullable: bool


class SQLProposalView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    intent: str
    sql: str
    dialect: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    metrics: tuple[str, ...]
    business_rules: tuple[str, ...]
    assumptions: tuple[str, ...]
    parameters: tuple[QueryParameterDefinitionView, ...]
    ambiguities: tuple[str, ...]
    needs_clarification: bool


class IntentEntityResolutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    term: str
    object_ref: str | None
    role: str
    confidence: float
    reason: str
    alternatives: tuple[str, ...]


class QueryIntentInterpretationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    summary: str
    requested_row_limit: int | None
    entities: tuple[IntentEntityResolutionView, ...]


class IntentMemoryCorrectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=300)
    role: str = Field(
        pattern=(
            r"^(primary_table|related_table|selected_column|filter_column|"
            r"grouping_column|ordering_column)$"
        )
    )
    corrected_object_ref: str = Field(min_length=3, max_length=700)
    previous_object_ref: str | None = Field(default=None, min_length=3, max_length=700)
    reason: str | None = Field(default=None, max_length=8_000)


class IntentMemoryCorrectionResultView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    memory_action: str
    definition: BusinessConceptDefinitionView
    resolution: BusinessConceptResolutionView
    query_request_state: QueryRequestState
    requires_regeneration: bool


class CurrentIntentEntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=500)
    role: str = Field(
        pattern=(
            r"^(primary_table|related_table|selected_column|filter_column|"
            r"grouping_column|ordering_column)$"
        )
    )
    object_ref: str | None = Field(default=None, min_length=3, max_length=700)


class FreeTextIntentCorrectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=200)
    correction_text: str = Field(min_length=1, max_length=10_000)
    correction_classification: Classification
    current_entities: tuple[CurrentIntentEntityCreate, ...] = Field(
        min_length=1,
        max_length=50,
    )


class IntentCorrectionInterpretationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entity_index: int
    term_to_remember: str
    corrected_object_ref: str | None
    confidence: float
    reason: str
    alternatives: tuple[str, ...]
    ambiguities: tuple[str, ...]
    needs_clarification: bool


class IntentCorrectionRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider_id: str
    model_id: str
    interpretation: IntentCorrectionInterpretationView
    memory_correction: IntentMemoryCorrectionResultView | None
    usage: LLMUsageView
    redacted_content_ids: tuple[str, ...]


class ValidationIssueView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    message: str
    blocking: bool


class OutputColumnLineageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    output_name: str
    source_columns: tuple[str, ...]


class ValidationResultView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dialect: str
    normalized_sql: str | None
    issues: tuple[ValidationIssueView, ...]
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    output_lineage: tuple[OutputColumnLineageView, ...]
    output_lineage_complete: bool


class SQLGenerationRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: str
    state: QueryRequestState
    catalog_version_id: str
    provider_id: str
    model_id: str
    context: SchemaContextView
    interpretation: QueryIntentInterpretationView
    proposal: SQLProposalView
    validation: ValidationResultView
    usage: LLMUsageView
    redacted_content_ids: tuple[str, ...]
    privacy_mode: GenerationPrivacyMode
    generation_strategy: GenerationStrategy
    generation_attempt_count: int
    fallback_reason: str | None
    validation_status: str
    ready_for_preview: bool
    ready_for_execution: bool
    transfer_receipt: AITransferReceiptView | None


class QueryParameterBindings(BaseModel):
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class QueryApprovalCreate(QueryParameterBindings):
    model_config = ConfigDict(extra="forbid")


class QueryRequestStateView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    state: QueryRequestState
    approved_by: str | None
    approved_at: datetime | None
    parameter_names: tuple[str, ...]
    parameter_definitions: tuple[QueryParameterDefinitionView, ...]
    updated_at: datetime


class ExplainResultView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan: dict[str, Any]
    estimated_total_cost: float | None
    estimated_rows: int | None
    elapsed_ms: int


class ReadOnlyResultView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    row_count: int
    truncated: bool
    truncation_reason: str | None
    result_bytes: int
    elapsed_ms: int


class DeterministicAnswerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    shape: ResultShape
    summary: str
    deterministic: bool


class ClassificationCountView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    classification: Classification
    column_count: int


class ResultPrivacyReportView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    processing_mode: str
    maximum_classification: Classification
    classification_counts: tuple[ClassificationCountView, ...]
    raw_rows_sent_to_llm: bool
    llm_interpretation_used: bool
    masked_output_columns: tuple[str, ...]
    output_lineage_complete: bool
    warnings: tuple[str, ...]


class ResultProvenanceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: str
    data_source_id: str
    data_source_name: str
    catalog_version_id: str
    sql: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    metrics: tuple[str, ...]
    business_rules: tuple[str, ...]
    assumptions: tuple[str, ...]
    approved_by: str | None
    approved_at: datetime | None
    executed_at: datetime
    provider_id: str | None
    model_id: str | None
    llm_usage_event_id: str | None
    estimated_llm_cost: str | None
    actual_llm_cost: str | None
    estimated_db_cost: float | None
    estimated_db_rows: int | None
    row_count: int
    truncated: bool
    truncation_reason: str | None
    result_bytes: int
    execution_elapsed_ms: int
    parameter_names: tuple[str, ...]


class QueryExecutionRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    query_request: QueryRequestStateView
    result: ReadOnlyResultView
    answer: DeterministicAnswerView
    privacy: ResultPrivacyReportView
    provenance: ResultProvenanceView


def _bounded_environment_integer(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = os.environ.get(name, "").strip()
    try:
        value = default if not raw_value else int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _environment_boolean(name: str, *, default: bool) -> bool:
    raw_value = os.environ.get(name, "").strip().casefold()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _provider_deployment_metadata(
    providers: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    allowed_retention_modes = {mode.value for mode in ProviderRetentionMode}
    for provider_id, provider in providers.items():
        prefix = f"SQLVERITY_{provider_id.upper()}"
        default_residency = "local" if provider_id == "ollama" else "unspecified"
        default_retention = (
            ProviderRetentionMode.LOCAL_RUNTIME.value
            if provider_id == "ollama"
            else ProviderRetentionMode.PROVIDER_DEFAULT.value
        )
        residency = os.environ.get(
            f"{prefix}_DATA_RESIDENCY",
            default_residency,
        ).strip().casefold()
        retention = os.environ.get(
            f"{prefix}_RETENTION_MODE",
            default_retention,
        ).strip().casefold()
        if not residency or retention not in allowed_retention_modes:
            raise RuntimeError(f"Invalid deployment metadata for provider {provider_id}")
        default_deployment_type = (
            "local_private" if provider_id == "ollama" else "external_cloud"
        )
        deployment_type = os.environ.get(
            f"{prefix}_DEPLOYMENT_TYPE",
            default_deployment_type,
        ).strip().casefold()
        if deployment_type not in {"external_cloud", "local_private"}:
            raise RuntimeError(f"Invalid deployment type for provider {provider_id}")
        capabilities = (
            provider.capabilities()
            if callable(getattr(provider, "capabilities", None))
            else {}
        )
        model_value = capabilities.get("model_id") if isinstance(capabilities, Mapping) else None
        model_id = (
            model_value.strip()
            if isinstance(model_value, str) and model_value.strip()
            else "unreported"
        )
        metadata[provider_id] = {
            "data_residency": residency,
            "retention_mode": retention,
            "deployment_type": deployment_type,
            "model_id": model_id,
        }
    return metadata


def _request_quota_limits() -> RequestQuotaLimits:
    return RequestQuotaLimits(
        window_seconds=_bounded_environment_integer(
            "SQLVERITY_RATE_WINDOW_SECONDS",
            default=60,
            minimum=1,
            maximum=3_600,
        ),
        user=ScopeQuota(
            requests_per_window=_bounded_environment_integer(
                "SQLVERITY_USER_REQUESTS_PER_WINDOW",
                default=60,
                minimum=1,
                maximum=1_000_000,
            ),
            max_concurrent=_bounded_environment_integer(
                "SQLVERITY_USER_MAX_CONCURRENT",
                default=4,
                minimum=1,
                maximum=1_000,
            ),
        ),
        tenant=ScopeQuota(
            requests_per_window=_bounded_environment_integer(
                "SQLVERITY_TENANT_REQUESTS_PER_WINDOW",
                default=300,
                minimum=1,
                maximum=1_000_000,
            ),
            max_concurrent=_bounded_environment_integer(
                "SQLVERITY_TENANT_MAX_CONCURRENT",
                default=20,
                minimum=1,
                maximum=10_000,
            ),
        ),
        data_source=ScopeQuota(
            requests_per_window=_bounded_environment_integer(
                "SQLVERITY_DATA_SOURCE_REQUESTS_PER_WINDOW",
                default=120,
                minimum=1,
                maximum=1_000_000,
            ),
            max_concurrent=_bounded_environment_integer(
                "SQLVERITY_DATA_SOURCE_MAX_CONCURRENT",
                default=8,
                minimum=1,
                maximum=10_000,
            ),
        ),
    )


def _semantic_inference_job_handler(
    service: SemanticInferenceService,
    *,
    batch_size: int,
) -> Callable[[BackgroundJob], JobExecutionOutcome]:
    def handle(job: BackgroundJob) -> JobExecutionOutcome:
        if job.data_source_id is None:
            raise ValueError("Semantic inference job requires a DataSource")
        provider_id = job.payload.get("provider_id")
        after_object_ref = job.payload.get("after_object_ref")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("Semantic inference job provider is invalid")
        if after_object_ref is not None and not isinstance(after_object_ref, str):
            raise ValueError("Semantic inference job cursor is invalid")
        try:
            run = service.infer_missing_descriptions(
                tenant_id=job.tenant_id,
                data_source_id=job.data_source_id,
                provider_id=provider_id,
                batch_size=batch_size,
                after_object_ref=after_object_ref,
            )
        except SemanticInferenceNoTargetsError:
            return JobExecutionOutcome(result={"status": "no_targets"})
        continuation = (
            {
                "provider_id": provider_id,
                "after_object_ref": run.last_target_ref,
            }
            if run.remaining_target_count > 0 and run.last_target_ref is not None
            else None
        )
        return JobExecutionOutcome(
            result={
                "catalog_version_id": run.catalog_version_id,
                "provider_id": run.provider_id,
                "model_id": run.model_id,
                "proposal_count": len(run.proposals),
                "redacted_count": len(run.redacted_object_refs),
                "remaining_target_count": run.remaining_target_count,
            },
            continuation_payload=continuation,
        )

    return handle


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    llm_providers = load_llm_providers_from_environment()
    secret_resolver = load_secret_resolver_from_environment()
    oidc_authenticator = load_oidc_authenticator_from_environment()
    request_tracer = load_request_tracer_from_environment()
    repository, catalog_backend = load_catalog_repository_from_environment(
        secret_resolver
    )
    repository.initialize()
    preflight_signing_key_value = os.environ.get("SQLVERITY_PREFLIGHT_SIGNING_KEY")
    if catalog_backend == "postgresql" and not preflight_signing_key_value:
        raise RuntimeError(
            "SQLVERITY_PREFLIGHT_SIGNING_KEY is required with the PostgreSQL catalog backend"
        )
    provider_deployment_metadata = _provider_deployment_metadata(llm_providers)
    policy_engine = SchemaQuestionPolicyEngine(
        allowed_provider_ids=frozenset(llm_providers),
        provider_policy_repository=repository,
        provider_metadata=provider_deployment_metadata,
        require_explicit_provider_policy=_environment_boolean(
            "SQLVERITY_REQUIRE_PROVIDER_POLICY",
            default=True,
        ),
    )
    sql_validator = SQLValidatorRegistry()
    finops = FinOpsService(repository)
    app.state.catalog = repository
    app.state.catalog_backend = catalog_backend
    app.state.llm_provider_ids = tuple(sorted(llm_providers))
    app.state.provider_deployment_metadata = provider_deployment_metadata
    app.state.security = AuthenticationService(
        repository,
        os.environ.get("SQLVERITY_BOOTSTRAP_API_KEY"),
        oidc_authenticator,
    )
    oidc_browser = load_oidc_browser_flow_from_environment(
        oidc_authenticator
    )
    app.state.oidc_browser = oidc_browser
    app.state.text_classifier = ServerSideTextClassifier()
    app.state.metrics = OperationalMetrics()
    app.state.request_tracer = request_tracer
    app.state.request_quotas = RequestQuotaManager(repository, _request_quota_limits())
    app.state.ingestion = CatalogIngestionService(
        repository,
        {
            "postgres": PostgreSQLConnector(secret_resolver),
            "postgresql": PostgreSQLConnector(secret_resolver),
            "mysql": MySQLConnector(secret_resolver, dialect="mysql"),
            "mariadb": MySQLConnector(secret_resolver, dialect="mariadb"),
            "oracle": OracleConnector(secret_resolver),
            "mssql": SQLServerConnector(secret_resolver),
            "sqlserver": SQLServerConnector(secret_resolver),
            "tsql": SQLServerConnector(secret_resolver),
        },
    )
    app.state.offline_import = OfflineSchemaImportService(
        repository,
        app.state.ingestion,
    )
    app.state.authorized_queries = AuthorizedQueryService(
        repository,
        app.state.ingestion,
    )
    app.state.schema_explorer = SchemaExplorerService(repository)
    app.state.semantic_governance = SemanticGovernanceService(repository)
    app.state.business_concepts = BusinessConceptService(repository)
    app.state.intent_memory = IntentMemoryService(
        repository,
        app.state.business_concepts,
    )
    app.state.analytics_semantics = AnalyticsSemanticsService(
        repository,
        app.state.business_concepts,
    )
    app.state.llm_gateway = LLMGateway(
        llm_providers,
        policy_engine,
        repository,
        finops,
    )
    app.state.intent_correction_interpreter = IntentCorrectionInterpreterService(
        repository,
        app.state.llm_gateway,
        app.state.intent_memory,
    )
    app.state.semantic_inference = SemanticInferenceService(
        repository,
        app.state.llm_gateway,
    )
    background_worker: DurableJobWorker | None = None
    if _environment_boolean("SQLVERITY_BACKGROUND_WORKER_ENABLED", default=False):
        background_worker = DurableJobWorker(
            repository,
            {
                "semantic_inference": _semantic_inference_job_handler(
                    app.state.semantic_inference,
                    batch_size=_bounded_environment_integer(
                        "SQLVERITY_SEMANTIC_INFERENCE_BATCH_SIZE",
                        default=50,
                        minimum=1,
                        maximum=200,
                    ),
                )
            },
            poll_seconds=(
                _bounded_environment_integer(
                    "SQLVERITY_BACKGROUND_WORKER_POLL_MILLISECONDS",
                    default=1000,
                    minimum=50,
                    maximum=60_000,
                )
                / 1000
            ),
            lease_seconds=_bounded_environment_integer(
                "SQLVERITY_BACKGROUND_JOB_LEASE_SECONDS",
                default=120,
                minimum=15,
                maximum=3_600,
            ),
        )
        background_worker.start()
    app.state.background_worker = background_worker
    app.state.learning_loop = LearningLoopService(repository, sql_validator)
    app.state.learning_governance = LearningGovernanceService(
        repository,
        app.state.learning_loop,
    )
    app.state.context_builder = ContextBuilderService(
        repository,
        app.state.learning_loop,
        app.state.business_concepts,
        app.state.analytics_semantics,
    )
    app.state.sql_generation = SQLGenerationService(
        app.state.context_builder,
        app.state.llm_gateway,
        sql_validator,
        repository,
        confirmation_manager=PreflightConfirmationManager(
            (
                preflight_signing_key_value.encode("utf-8")
                if preflight_signing_key_value
                else None
            ),
            ttl_seconds=_bounded_environment_integer(
                "SQLVERITY_PREFLIGHT_TTL_SECONDS",
                default=300,
                minimum=30,
                maximum=3_600,
            ),
            confirmation_store=repository,
        ),
        receipt_recorder=repository,
    )
    app.state.query_execution = QueryExecutionService(
        repository,
        sql_validator,
        policy_engine,
        {
            "postgres": PostgreSQLReadOnlyExecutor(secret_resolver),
            "postgresql": PostgreSQLReadOnlyExecutor(secret_resolver),
            "mysql": MySQLReadOnlyExecutor(secret_resolver, dialect="mysql"),
            "mariadb": MySQLReadOnlyExecutor(secret_resolver, dialect="mariadb"),
            "oracle": OracleReadOnlyExecutor(secret_resolver),
            "mssql": SQLServerReadOnlyExecutor(secret_resolver),
            "sqlserver": SQLServerReadOnlyExecutor(secret_resolver),
            "tsql": SQLServerReadOnlyExecutor(secret_resolver),
        },
        DeterministicResultProcessor(),
    )
    app.state.finops = finops
    try:
        yield
    finally:
        with ExitStack() as cleanup:
            cleanup.callback(repository.close)
            cleanup.callback(request_tracer.shutdown)
            cleanup.callback(secret_resolver.close)
            cleanup.callback(cast(LLMGateway, app.state.llm_gateway).close)
            if oidc_browser is not None:
                cleanup.callback(oidc_browser.close)
            if background_worker is not None:
                cleanup.callback(background_worker.stop)


class SQLVerityAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is not None:
            return self.openapi_schema
        schema = get_openapi(
            title=self.title,
            version=self.version,
            description=self.description,
            routes=self.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "SQLVerity AI API key",
        }
        schema["security"] = [{"BearerAuth": []}]
        schema["paths"]["/health"]["get"]["security"] = []
        schema["paths"]["/health/ready"]["get"]["security"] = []
        self.openapi_schema = schema
        return schema


app = SQLVerityAPI(
    title="SQLVerity AI API",
    version="0.1.0",
    description="Governed natural-language to SQL platform",
    lifespan=lifespan,
)

_WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
_CONSOLE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_OIDC_FLOW_COOKIE = "sqlverity_oidc_flow"
_SESSION_COOKIE = "sqlverity_session"
_CSRF_COOKIE = "sqlverity_csrf"
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
app.mount("/ui/assets", StaticFiles(directory=_WEB_ROOT / "assets"), name="ui-assets")


@app.middleware("http")
async def observe_api_request(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    metrics = cast(OperationalMetrics, request.app.state.metrics)
    request_tracer = cast(RequestTracer, request.app.state.request_tracer)
    request_trace = request_tracer.start_request(request.headers, method=request.method)
    request_id = str(uuid4())
    request.state.request_id = request_id
    request.state.trace_id = request_trace.trace_id
    started_at = perf_counter()
    status_code = 500
    response: Response | None = None
    metrics.begin_request()
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        trace_headers = request_trace.finish(
            route=str(route_path),
            status_code=status_code,
        )
        if response is not None:
            for name, value in trace_headers.items():
                response.headers[name] = value
        metrics.end_request(
            RequestObservation(
                method=request.method,
                route=str(route_path),
                status_code=status_code,
                elapsed_seconds=perf_counter() - started_at,
                request_id=request_id,
                trace_id=request_trace.trace_id,
            )
        )


@app.middleware("http")
async def authenticate_api_request(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    if not request.url.path.startswith("/v1/"):
        return await call_next(request)
    security = cast(AuthenticationService, request.app.state.security)
    quota_lease: RequestQuotaLease | None = None
    try:
        authorization = request.headers.get("Authorization")
        cookie_authenticated = authorization is None and bool(
            request.cookies.get(_SESSION_COOKIE)
        )
        if cookie_authenticated:
            authorization = f"Bearer {request.cookies[_SESSION_COOKIE]}"
            if request.method not in _SAFE_HTTP_METHODS:
                csrf_cookie = request.cookies.get(_CSRF_COOKIE, "")
                csrf_header = request.headers.get("X-CSRF-Token", "")
                if (
                    not csrf_cookie
                    or not csrf_header
                    or not secrets.compare_digest(csrf_cookie, csrf_header)
                ):
                    raise AuthorizationError("Browser session CSRF validation failed")
        principal = security.authenticate_bearer(authorization)
        request.state.principal = principal
        segments = tuple(
            segment for segment in request.url.path.split("/") if segment
        )
        if len(segments) >= 3 and segments[:2] == ("v1", "tenants"):
            tenant_id = segments[2]
            data_source_id = (
                segments[4]
                if len(segments) >= 5 and segments[3] == "data-sources"
                else None
            )
            security.authorize(
                principal,
                SecurityPermission.READ,
                tenant_id=tenant_id,
                data_source_id=data_source_id,
            )
        else:
            tenant_id = None
            data_source_id = None
        quota_manager = cast(RequestQuotaManager, request.app.state.request_quotas)
        quota = quota_manager.acquire(
            principal_id=principal.id,
            tenant_id=tenant_id,
            data_source_id=data_source_id,
        )
        if not quota.allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": (
                        f"Request {quota.reason or 'quota'} limit exceeded "
                        f"for {quota.denied_scope or 'request'} scope"
                    )
                },
                headers={"Retry-After": str(quota.retry_after_seconds)},
            )
        quota_lease = quota.lease
    except AuthenticationError as error:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(error)},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except AuthorizationError as error:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": str(error)},
        )
    try:
        return await call_next(request)
    finally:
        if quota_lease is not None:
            cast(RequestQuotaManager, request.app.state.request_quotas).release(
                quota_lease
            )


def get_catalog(request: Request) -> SQLiteCatalogRepository:
    return cast(SQLiteCatalogRepository, request.app.state.catalog)


def get_security(request: Request) -> AuthenticationService:
    return cast(AuthenticationService, request.app.state.security)


def get_authenticated_principal(request: Request) -> AuthenticatedPrincipal:
    return cast(AuthenticatedPrincipal, request.state.principal)


def require_permission(
    permission: SecurityPermission,
) -> Callable[[Request], AuthenticatedPrincipal]:
    def dependency(request: Request) -> AuthenticatedPrincipal:
        principal = get_authenticated_principal(request)
        tenant_id = request.path_params.get("tenant_id")
        data_source_id = request.path_params.get("data_source_id")
        try:
            get_security(request).authorize(
                principal,
                permission,
                tenant_id=tenant_id,
                data_source_id=data_source_id,
            )
        except AuthorizationError as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(error),
            ) from error
        return principal

    return dependency


def get_ingestion(request: Request) -> CatalogIngestionService:
    return cast(CatalogIngestionService, request.app.state.ingestion)


def get_offline_import(request: Request) -> OfflineSchemaImportService:
    return cast(OfflineSchemaImportService, request.app.state.offline_import)


def get_schema_explorer(request: Request) -> SchemaExplorerService:
    return cast(SchemaExplorerService, request.app.state.schema_explorer)


def get_semantic_governance(request: Request) -> SemanticGovernanceService:
    return cast(SemanticGovernanceService, request.app.state.semantic_governance)


def get_semantic_inference(request: Request) -> SemanticInferenceService:
    return cast(SemanticInferenceService, request.app.state.semantic_inference)


def get_business_concepts(request: Request) -> BusinessConceptService:
    return cast(BusinessConceptService, request.app.state.business_concepts)


def get_intent_memory(request: Request) -> IntentMemoryService:
    return cast(IntentMemoryService, request.app.state.intent_memory)


def get_intent_correction_interpreter(
    request: Request,
) -> IntentCorrectionInterpreterService:
    return cast(
        IntentCorrectionInterpreterService,
        request.app.state.intent_correction_interpreter,
    )


def get_analytics_semantics(request: Request) -> AnalyticsSemanticsService:
    return cast(AnalyticsSemanticsService, request.app.state.analytics_semantics)


def get_context_builder(request: Request) -> ContextBuilderService:
    return cast(ContextBuilderService, request.app.state.context_builder)


def get_learning_loop(request: Request) -> LearningLoopService:
    return cast(LearningLoopService, request.app.state.learning_loop)


def get_learning_governance(request: Request) -> LearningGovernanceService:
    return cast(
        LearningGovernanceService,
        request.app.state.learning_governance,
    )


def get_sql_generation(request: Request) -> SQLGenerationService:
    return cast(SQLGenerationService, request.app.state.sql_generation)


def get_query_execution(request: Request) -> QueryExecutionService:
    return cast(QueryExecutionService, request.app.state.query_execution)


def get_finops(request: Request) -> FinOpsService:
    return cast(FinOpsService, request.app.state.finops)


def get_authorized_queries(request: Request) -> AuthorizedQueryService:
    return cast(AuthorizedQueryService, request.app.state.authorized_queries)


CatalogDependency = Annotated[SQLiteCatalogRepository, Depends(get_catalog)]
SecurityDependency = Annotated[AuthenticationService, Depends(get_security)]
PlatformAdminDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.PLATFORM_MANAGE)),
]
SecurityAdminDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.SECURITY_MANAGE)),
]
DataSourceManagerDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.DATA_SOURCE_MANAGE)),
]
SemanticManagerDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.SEMANTIC_MANAGE)),
]
QueryUserDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.QUERY_USE)),
]
QueryApproverDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.QUERY_APPROVE)),
]
FeedbackWriterDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.FEEDBACK_WRITE)),
]
GoldenReviewerDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.GOLDEN_REVIEW)),
]
FinOpsManagerDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.FINOPS_MANAGE)),
]
AuditReaderDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_permission(SecurityPermission.AUDIT_READ)),
]
IngestionDependency = Annotated[CatalogIngestionService, Depends(get_ingestion)]
OfflineImportDependency = Annotated[OfflineSchemaImportService, Depends(get_offline_import)]
SchemaExplorerDependency = Annotated[SchemaExplorerService, Depends(get_schema_explorer)]
SemanticGovernanceDependency = Annotated[
    SemanticGovernanceService,
    Depends(get_semantic_governance),
]
SemanticInferenceDependency = Annotated[
    SemanticInferenceService,
    Depends(get_semantic_inference),
]
BusinessConceptDependency = Annotated[
    BusinessConceptService,
    Depends(get_business_concepts),
]
IntentMemoryDependency = Annotated[
    IntentMemoryService,
    Depends(get_intent_memory),
]
IntentCorrectionInterpreterDependency = Annotated[
    IntentCorrectionInterpreterService,
    Depends(get_intent_correction_interpreter),
]
AnalyticsSemanticsDependency = Annotated[
    AnalyticsSemanticsService,
    Depends(get_analytics_semantics),
]
ContextBuilderDependency = Annotated[
    ContextBuilderService,
    Depends(get_context_builder),
]
LearningLoopDependency = Annotated[LearningLoopService, Depends(get_learning_loop)]
LearningGovernanceDependency = Annotated[
    LearningGovernanceService,
    Depends(get_learning_governance),
]
SQLGenerationDependency = Annotated[
    SQLGenerationService,
    Depends(get_sql_generation),
]
QueryExecutionDependency = Annotated[
    QueryExecutionService,
    Depends(get_query_execution),
]
FinOpsDependency = Annotated[FinOpsService, Depends(get_finops)]
AuthorizedQueryDependency = Annotated[
    AuthorizedQueryService,
    Depends(get_authorized_queries),
]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness(request: Request) -> Response:
    try:
        healthy = cast(SQLiteCatalogRepository, request.app.state.catalog).health_check()
    except Exception:
        healthy = False
    worker = cast(DurableJobWorker | None, request.app.state.background_worker)
    worker_healthy = worker is None or worker.health_check()
    if not healthy or not worker_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "catalog": "available" if healthy else "unavailable",
                "worker": "available" if worker_healthy else "unavailable",
            },
        )
    return JSONResponse(
        content={
            "status": "ready",
            "catalog": "available",
            "worker": "disabled" if worker is None else "available",
        }
    )


@app.get(
    "/auth/oidc/config",
    response_model=BrowserAuthConfigView,
    include_in_schema=False,
)
def oidc_browser_config(request: Request) -> BrowserAuthConfigView:
    flow = cast(OIDCBrowserFlow | None, request.app.state.oidc_browser)
    return BrowserAuthConfigView(
        enabled=flow is not None,
        login_url="/auth/oidc/login" if flow is not None else None,
    )


@app.get("/auth/oidc/login", include_in_schema=False)
def begin_oidc_login(request: Request) -> Response:
    flow = cast(OIDCBrowserFlow | None, request.app.state.oidc_browser)
    if flow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC login is disabled")
    login = flow.begin_login()
    response = RedirectResponse(login.authorization_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        _OIDC_FLOW_COOKIE,
        login.flow_cookie,
        max_age=600,
        httponly=True,
        secure=flow.settings.secure_cookies,
        samesite="lax",
        path="/auth/oidc",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/oidc/callback", include_in_schema=False)
def complete_oidc_login(
    request: Request,
    code: str = "",
    state: str = "",
    error: str | None = None,
) -> Response:
    flow = cast(OIDCBrowserFlow | None, request.app.state.oidc_browser)
    if flow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC login is disabled")
    if error is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="OIDC login failed")
    flow_cookie = request.cookies.get(_OIDC_FLOW_COOKIE, "")
    try:
        id_token = flow.exchange_callback(
            code=code,
            state=state,
            flow_cookie=flow_cookie,
        )
        cast(AuthenticationService, request.app.state.security).authenticate_bearer(
            f"Bearer {id_token}"
        )
    except (OIDCAuthenticationError, AuthenticationError) as auth_error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC login could not be completed",
        ) from auth_error
    csrf_token = secrets.token_urlsafe(32)
    response = RedirectResponse("/ui?oidc=connected", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(_OIDC_FLOW_COOKIE, path="/auth/oidc")
    response.set_cookie(
        _SESSION_COOKIE,
        id_token,
        max_age=3600,
        httponly=True,
        secure=flow.settings.secure_cookies,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        _CSRF_COOKIE,
        csrf_token,
        max_age=3600,
        httponly=False,
        secure=flow.settings.secure_cookies,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get(
    "/auth/oidc/session",
    response_model=BrowserSessionView,
    include_in_schema=False,
)
def get_oidc_browser_session(request: Request) -> BrowserSessionView:
    token = request.cookies.get(_SESSION_COOKIE)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No browser session")
    try:
        principal = cast(
            AuthenticationService, request.app.state.security
        ).authenticate_bearer(f"Bearer {token}")
    except AuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Browser session is invalid or expired",
        ) from error
    return BrowserSessionView(
        principal_id=principal.id,
        subject=principal.subject,
        display_name=principal.display_name,
        tenant_id=principal.tenant_id,
        authentication_method=principal.authentication_method,
        mfa_verified=principal.mfa_verified,
    )


@app.post("/auth/oidc/logout", include_in_schema=False)
def end_oidc_browser_session(request: Request) -> Response:
    csrf_cookie = request.cookies.get(_CSRF_COOKIE, "")
    csrf_header = request.headers.get("X-CSRF-Token", "")
    if (
        not csrf_cookie
        or not csrf_header
        or not secrets.compare_digest(csrf_cookie, csrf_header)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Browser session CSRF validation failed",
        )
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(_SESSION_COOKIE, path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
def web_console() -> FileResponse:
    return FileResponse(_WEB_ROOT / "index.html", headers=_CONSOLE_HEADERS)


@app.get("/v1/system/capabilities", response_model=SystemCapabilitiesView)
def system_capabilities(
    request: Request,
    _platform_admin: PlatformAdminDependency,
) -> SystemCapabilitiesView:
    return SystemCapabilitiesView(
        service_version=app.version,
        catalog_backend=cast(str, request.app.state.catalog_backend),
        supported_dialects=DEFAULT_DIALECT_REGISTRY.dialects,
        configured_provider_ids=cast(tuple[str, ...], request.app.state.llm_provider_ids),
    )


@app.get("/v1/system/metrics", response_class=PlainTextResponse)
def prometheus_metrics(
    request: Request,
    _platform_admin: PlatformAdminDependency,
) -> str:
    worker = cast(DurableJobWorker | None, request.app.state.background_worker)
    return cast(OperationalMetrics, request.app.state.metrics).render_prometheus(
        worker_enabled=worker is not None,
        worker_healthy=worker.health_check() if worker is not None else False,
        worker_last_poll_age_seconds=(
            worker.last_poll_age_seconds if worker is not None else None
        ),
    )


@app.post("/v1/tenants", response_model=TenantView, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    catalog: CatalogDependency,
    _platform_admin: PlatformAdminDependency,
) -> Tenant:
    return catalog.create_tenant(payload.name)


@app.get("/v1/tenants", response_model=tuple[TenantView, ...])
def list_tenants(
    catalog: CatalogDependency,
    _platform_admin: PlatformAdminDependency,
) -> tuple[Tenant, ...]:
    return catalog.list_tenants()


@app.get(
    "/v1/tenants/{tenant_id}/audit/events",
    response_model=tuple[AuditEventView, ...],
)
def list_audit_events(
    tenant_id: str,
    catalog: CatalogDependency,
    _actor: AuditReaderDependency,
) -> tuple[AuditEvent, ...]:
    if catalog.get_tenant(tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return catalog.audit_events(tenant_id)


@app.get(
    "/v1/tenants/{tenant_id}/audit/export",
    response_class=PlainTextResponse,
)
def export_audit_events(
    tenant_id: str,
    catalog: CatalogDependency,
    _actor: AuditReaderDependency,
) -> PlainTextResponse:
    if catalog.get_tenant(tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    lines = (
        json.dumps(
            {
                "id": event.id,
                "tenant_id": event.tenant_id,
                "event_type": event.event_type,
                "subject_type": event.subject_type,
                "subject_id": event.subject_id,
                "details": dict(event.details),
                "created_at": event.created_at.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        for event in catalog.audit_events(tenant_id)
    )
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="sqlverity-audit-{tenant_id}.ndjson"'
        },
    )


@app.post(
    "/v1/tenants/{tenant_id}/security/principals",
    response_model=IssuedAccessView,
    status_code=status.HTTP_201_CREATED,
)
def provision_security_principal(
    tenant_id: str,
    payload: SecurityPrincipalCreate,
    security: SecurityDependency,
    actor: SecurityAdminDependency,
) -> IssuedAccess:
    try:
        return security.provision_principal(
            tenant_id=tenant_id,
            subject=payload.subject,
            display_name=payload.display_name,
            role=payload.role,
            credential_label=payload.credential_label,
            data_source_ids=payload.data_source_ids,
            expires_at=payload.expires_at,
            created_by=actor.actor_id,
        )
    except (LookupError, DataSourceNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SecurityAccessConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    "/v1/tenants/{tenant_id}/security/principals",
    response_model=tuple[PrincipalAccessView, ...],
)
def list_security_principals(
    tenant_id: str,
    security: SecurityDependency,
    _actor: SecurityAdminDependency,
) -> tuple[PrincipalAccess, ...]:
    return security.list_principals(tenant_id)


@app.post(
    "/v1/tenants/{tenant_id}/security/federated-principals",
    response_model=PrincipalAccessView,
    status_code=status.HTTP_201_CREATED,
)
def provision_federated_security_principal(
    tenant_id: str,
    payload: FederatedPrincipalCreate,
    security: SecurityDependency,
    actor: SecurityAdminDependency,
) -> PrincipalAccess:
    try:
        return security.provision_federated_principal(
            tenant_id=tenant_id,
            subject=payload.subject,
            display_name=payload.display_name,
            role=payload.role,
            data_source_ids=payload.data_source_ids,
            created_by=actor.actor_id,
        )
    except (LookupError, DataSourceNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SecurityAccessConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/security/credentials/{credential_id}/revocations",
    response_model=CredentialRevocationView,
    status_code=status.HTTP_201_CREATED,
)
def revoke_api_credential(
    tenant_id: str,
    credential_id: str,
    payload: CredentialRevocationCreate,
    security: SecurityDependency,
    actor: SecurityAdminDependency,
) -> APICredentialRevocation:
    try:
        return security.revoke_credential(
            tenant_id=tenant_id,
            credential_id=credential_id,
            actor_id=actor.actor_id,
            reason=payload.reason,
        )
    except CredentialNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SecurityAccessConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/finops/model-pricing",
    response_model=ModelPricingView,
    status_code=status.HTTP_201_CREATED,
)
def create_model_pricing(
    tenant_id: str,
    payload: ModelPricingCreate,
    catalog: CatalogDependency,
    _actor: FinOpsManagerDependency,
) -> ModelPricing:
    try:
        pricing = ModelPricing(tenant_id=tenant_id, **payload.model_dump())
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    try:
        return catalog.create_model_pricing(pricing)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/finops/model-pricing",
    response_model=tuple[ModelPricingView, ...],
)
def list_model_pricing(
    tenant_id: str,
    catalog: CatalogDependency,
) -> tuple[ModelPricing, ...]:
    if catalog.get_tenant(tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return catalog.list_model_pricing(tenant_id)


@app.post(
    "/v1/tenants/{tenant_id}/finops/budgets",
    response_model=TenantBudgetView,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant_budget(
    tenant_id: str,
    payload: TenantBudgetCreate,
    catalog: CatalogDependency,
    _actor: FinOpsManagerDependency,
) -> TenantBudget:
    try:
        budget = TenantBudget(tenant_id=tenant_id, **payload.model_dump())
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    try:
        return catalog.create_tenant_budget(budget)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/finops/budgets",
    response_model=tuple[TenantBudgetView, ...],
)
def list_tenant_budgets(
    tenant_id: str,
    catalog: CatalogDependency,
) -> tuple[TenantBudget, ...]:
    if catalog.get_tenant(tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return catalog.list_tenant_budgets(tenant_id)


@app.get(
    "/v1/tenants/{tenant_id}/provider-egress-policies",
    response_model=tuple[ProviderEgressPolicyView, ...],
)
def list_provider_egress_policies(
    tenant_id: str,
    catalog: CatalogDependency,
    _actor: SecurityAdminDependency,
) -> tuple[ProviderEgressPolicy, ...]:
    try:
        return catalog.list_provider_egress_policies(tenant_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.put(
    "/v1/tenants/{tenant_id}/provider-egress-policies/{provider_id}",
    response_model=ProviderEgressPolicyView,
)
def upsert_tenant_provider_egress_policy(
    tenant_id: str,
    provider_id: str,
    payload: ProviderEgressPolicyUpsert,
    catalog: CatalogDependency,
    request: Request,
    actor: SecurityAdminDependency,
) -> ProviderEgressPolicy:
    return _upsert_provider_egress_policy(
        catalog=catalog,
        tenant_id=tenant_id,
        data_source_id=None,
        provider_id=provider_id,
        payload=payload,
        actor_id=actor.actor_id,
        provider_metadata=cast(
            Mapping[str, Mapping[str, str]],
            request.app.state.provider_deployment_metadata,
        ),
    )


@app.put(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "provider-egress-policies/{provider_id}"
    ),
    response_model=ProviderEgressPolicyView,
)
def upsert_data_source_provider_egress_policy(
    tenant_id: str,
    data_source_id: str,
    provider_id: str,
    payload: ProviderEgressPolicyUpsert,
    catalog: CatalogDependency,
    request: Request,
    actor: DataSourceManagerDependency,
) -> ProviderEgressPolicy:
    return _upsert_provider_egress_policy(
        catalog=catalog,
        tenant_id=tenant_id,
        data_source_id=data_source_id,
        provider_id=provider_id,
        payload=payload,
        actor_id=actor.actor_id,
        provider_metadata=cast(
            Mapping[str, Mapping[str, str]],
            request.app.state.provider_deployment_metadata,
        ),
    )


def _upsert_provider_egress_policy(
    *,
    catalog: SQLiteCatalogRepository,
    tenant_id: str,
    data_source_id: str | None,
    provider_id: str,
    payload: ProviderEgressPolicyUpsert,
    actor_id: str,
    provider_metadata: Mapping[str, Mapping[str, str]],
) -> ProviderEgressPolicy:
    try:
        deployment = provider_metadata.get(provider_id)
        if payload.allowed and deployment is None:
            raise ValueError("Provider deployment is not configured")
        if payload.allowed and not payload.acknowledged:
            raise ValueError("Informed provider egress acknowledgement is required")
        acknowledgement_digest: str | None = None
        acknowledged_at: datetime | None = None
        acknowledged_by: str | None = None
        if payload.allowed:
            assert deployment is not None
            scope = "data_source" if data_source_id is not None else "tenant"
            acknowledgement_digest = policy_acknowledgement_digest(
                provider_id=provider_id,
                model_id=deployment["model_id"],
                allowed=payload.allowed,
                allowed_purposes=payload.allowed_purposes,
                maximum_classification=payload.maximum_classification,
                data_residency=payload.data_residency,
                retention_mode=payload.retention_mode.value,
                scope=scope,
                deployment_type=deployment["deployment_type"],
            )
            acknowledged_at = utc_now()
            acknowledged_by = actor_id
        policy = ProviderEgressPolicy(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            provider_id=provider_id,
            allowed=payload.allowed,
            maximum_classification=payload.maximum_classification,
            allowed_purposes=payload.allowed_purposes,
            data_residency=payload.data_residency,
            retention_mode=payload.retention_mode,
            acknowledgement_digest=acknowledgement_digest,
            acknowledged_at=acknowledged_at,
            acknowledged_by=acknowledged_by,
        )
        return catalog.upsert_provider_egress_policy(policy)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


def _effective_provider_privacy_view(
    *,
    catalog: SQLiteCatalogRepository,
    tenant_id: str,
    data_source_id: str,
    provider_id: str,
    provider_metadata: Mapping[str, Mapping[str, str]],
) -> EffectiveProviderPrivacyView:
    deployment = provider_metadata.get(provider_id)
    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider deployment is not configured",
        )
    policy = catalog.get_effective_provider_egress_policy(
        tenant_id,
        provider_id,
        data_source_id,
    )
    scope = (
        "none"
        if policy is None
        else ("data_source" if policy.data_source_id is not None else "tenant")
    )
    review_required = False
    if policy is not None and policy.allowed:
        expected = policy_acknowledgement_digest(
            provider_id=provider_id,
            model_id=deployment["model_id"],
            allowed=policy.allowed,
            allowed_purposes=policy.allowed_purposes,
            maximum_classification=policy.maximum_classification,
            data_residency=policy.data_residency,
            retention_mode=policy.retention_mode.value,
            scope=scope,
            deployment_type=deployment["deployment_type"],
        )
        review_required = policy.acknowledgement_digest != expected
    deployment_matches_policy = (
        policy is not None
        and policy.data_residency == deployment["data_residency"]
        and policy.retention_mode.value == deployment["retention_mode"]
    )
    if policy is None:
        decision_code = "missing_policy"
    elif not policy.allowed:
        decision_code = "denied_provider"
    elif policy.data_residency != deployment["data_residency"]:
        decision_code = "residency_mismatch"
    elif policy.retention_mode.value != deployment["retention_mode"]:
        decision_code = "retention_mismatch"
    elif review_required:
        decision_code = "policy_review_required"
    else:
        decision_code = "allowed"
    return EffectiveProviderPrivacyView(
        deployment=ProviderDeploymentView(
            provider_id=provider_id,
            model_id=deployment["model_id"],
            deployment_type=deployment["deployment_type"],
            data_residency=deployment["data_residency"],
            retention_mode=ProviderRetentionMode(deployment["retention_mode"]),
        ),
        policy=(
            ProviderEgressPolicyView.model_validate(policy)
            if policy is not None
            else None
        ),
        policy_scope=scope,
        review_required=review_required,
        deployment_matches_policy=deployment_matches_policy,
        decision_code=decision_code,
    )


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/privacy/providers",
    response_model=tuple[EffectiveProviderPrivacyView, ...],
)
def list_effective_provider_privacy(
    tenant_id: str,
    data_source_id: str,
    catalog: CatalogDependency,
    request: Request,
    _actor: QueryUserDependency,
) -> tuple[EffectiveProviderPrivacyView, ...]:
    if catalog.get_data_source(tenant_id, data_source_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DataSource not found")
    metadata = cast(
        Mapping[str, Mapping[str, str]],
        request.app.state.provider_deployment_metadata,
    )
    return tuple(
        _effective_provider_privacy_view(
            catalog=catalog,
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            provider_id=provider_id,
            provider_metadata=metadata,
        )
        for provider_id in sorted(metadata)
    )


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "privacy/effective-policy/{provider_id}"
    ),
    response_model=EffectiveProviderPrivacyView,
)
def get_effective_provider_privacy(
    tenant_id: str,
    data_source_id: str,
    provider_id: str,
    catalog: CatalogDependency,
    request: Request,
    _actor: QueryUserDependency,
) -> EffectiveProviderPrivacyView:
    if catalog.get_data_source(tenant_id, data_source_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DataSource not found")
    return _effective_provider_privacy_view(
        catalog=catalog,
        tenant_id=tenant_id,
        data_source_id=data_source_id,
        provider_id=provider_id,
        provider_metadata=cast(
            Mapping[str, Mapping[str, str]],
            request.app.state.provider_deployment_metadata,
        ),
    )


@app.get(
    "/v1/tenants/{tenant_id}/ai-transfer-receipts",
    response_model=tuple[AITransferReceiptView, ...],
)
def list_ai_transfer_receipts(
    tenant_id: str,
    catalog: CatalogDependency,
    _actor: AuditReaderDependency,
    data_source_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
) -> tuple[AITransferReceipt, ...]:
    return catalog.list_ai_transfer_receipts(
        tenant_id,
        data_source_id=data_source_id,
        limit=limit,
    )


@app.get(
    "/v1/tenants/{tenant_id}/finops/summary",
    response_model=FinOpsSummaryView,
)
def get_finops_summary(
    tenant_id: str,
    catalog: CatalogDependency,
    finops: FinOpsDependency,
    currency: Annotated[
        str,
        Query(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"),
    ] = "USD",
) -> FinOpsSummary:
    if catalog.get_tenant(tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return finops.summary(tenant_id=tenant_id, currency=currency)


@app.post(
    "/v1/tenants/{tenant_id}/data-sources",
    response_model=DataSourceView,
    status_code=status.HTTP_201_CREATED,
)
def create_data_source(
    tenant_id: str,
    payload: DataSourceCreate,
    catalog: CatalogDependency,
    _actor: DataSourceManagerDependency,
) -> DataSource:
    try:
        dialect = DEFAULT_DIALECT_REGISTRY.resolve(payload.dialect).dialect
        if (
            payload.source_type is DataSourceType.AUTHORIZED_QUERY
            and dialect != "postgresql"
        ):
            raise ValueError(
                "Authorized-query DataSources currently require PostgreSQL"
            )
        return catalog.create_data_source(
            tenant_id=tenant_id,
            name=payload.name,
            source_type=payload.source_type,
            dialect=dialect,
            capabilities=payload.capabilities,
            connection_secret_ref=payload.connection_secret_ref,
        )
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get("/v1/tenants/{tenant_id}/data-sources/{data_source_id}", response_model=DataSourceView)
def get_data_source(
    tenant_id: str,
    data_source_id: str,
    catalog: CatalogDependency,
) -> DataSource:
    data_source = catalog.get_data_source(tenant_id, data_source_id)
    if data_source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DataSource not found")
    return data_source


@app.get(
    "/v1/tenants/{tenant_id}/data-sources",
    response_model=tuple[DataSourceView, ...],
)
def list_data_sources(
    tenant_id: str,
    catalog: CatalogDependency,
) -> tuple[DataSource, ...]:
    try:
        return catalog.list_data_sources(tenant_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.put(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/execution-cost-policy",
    response_model=ExecutionCostPolicyView,
)
def upsert_execution_cost_policy(
    tenant_id: str,
    data_source_id: str,
    payload: ExecutionCostPolicyUpsert,
    catalog: CatalogDependency,
    _actor: DataSourceManagerDependency,
) -> ExecutionCostPolicy:
    try:
        policy = ExecutionCostPolicy(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            **payload.model_dump(),
        )
        return catalog.upsert_execution_cost_policy(policy)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/execution-cost-policy",
    response_model=ExecutionCostPolicyView,
)
def get_execution_cost_policy(
    tenant_id: str,
    data_source_id: str,
    catalog: CatalogDependency,
) -> ExecutionCostPolicy:
    policy = catalog.get_execution_cost_policy(tenant_id, data_source_id)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution cost policy not found",
        )
    return policy


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "authorized-query/definitions"
    ),
    response_model=AuthorizedQueryRegistrationView,
    status_code=status.HTTP_201_CREATED,
)
def create_authorized_query_definition(
    tenant_id: str,
    data_source_id: str,
    payload: AuthorizedQueryDefinitionCreate,
    authorized_queries: AuthorizedQueryDependency,
    _actor: DataSourceManagerDependency,
) -> AuthorizedQueryRegistration:
    try:
        return authorized_queries.register(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            virtual_schema=payload.virtual_schema,
            virtual_name=payload.virtual_name,
            description=payload.description,
            base_sql=payload.base_sql,
            parameters=tuple(
                AuthorizedQueryParameter(**parameter.model_dump())
                for parameter in payload.parameters
            ),
            output_columns=tuple(
                ColumnSnapshot(
                    name=column.name,
                    physical_type=column.physical_type,
                    ordinal=ordinal,
                    nullable=column.nullable,
                    comment=column.description,
                    classification=column.classification,
                )
                for ordinal, column in enumerate(payload.output_columns, start=1)
            ),
            allow_filtering=payload.allow_filtering,
            allow_aggregation=payload.allow_aggregation,
        )
    except AuthorizedQueryDataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except AuthorizedQueryConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (LookupError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "authorized-query/definitions"
    ),
    response_model=tuple[AuthorizedQueryDefinitionView, ...],
)
def list_authorized_query_definitions(
    tenant_id: str,
    data_source_id: str,
    catalog: CatalogDependency,
) -> tuple[AuthorizedQueryDefinition, ...]:
    data_source = catalog.get_data_source(tenant_id, data_source_id)
    if data_source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DataSource not found")
    if data_source.source_type is not DataSourceType.AUTHORIZED_QUERY:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="DataSource is not an authorized query source",
        )
    return catalog.list_authorized_query_definitions(tenant_id, data_source_id)


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/connection-tests",
    response_model=ConnectionTestView,
)
def test_data_source_connection(
    tenant_id: str,
    data_source_id: str,
    ingestion: IngestionDependency,
    _actor: DataSourceManagerDependency,
) -> ConnectionTestReport:
    try:
        return ingestion.test_connection(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (CatalogIngestionError, ConnectorConfigurationError, SecretResolutionError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except ConnectorUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/ingestions",
    response_model=IngestionView,
    status_code=status.HTTP_201_CREATED,
)
def ingest_data_source(
    tenant_id: str,
    data_source_id: str,
    ingestion: IngestionDependency,
    _actor: DataSourceManagerDependency,
) -> IngestionReport:
    try:
        return ingestion.ingest(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (CatalogIngestionError, ConnectorConfigurationError, SecretResolutionError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except ConnectorUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/imports/ddl",
    response_model=IngestionView,
    status_code=status.HTTP_201_CREATED,
)
def import_ddl(
    tenant_id: str,
    data_source_id: str,
    payload: DDLImportCreate,
    offline_import: OfflineImportDependency,
    _actor: DataSourceManagerDependency,
) -> IngestionReport:
    try:
        return offline_import.import_ddl(
            tenant_id,
            data_source_id,
            payload.ddl,
            default_schema=payload.default_schema,
        )
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (CatalogIngestionError, DDLParseError, OfflineImportModeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/imports/manual",
    response_model=IngestionView,
    status_code=status.HTTP_201_CREATED,
)
def import_manual_schema(
    tenant_id: str,
    data_source_id: str,
    payload: ManualImportCreate,
    offline_import: OfflineImportDependency,
    catalog: CatalogDependency,
    _actor: DataSourceManagerDependency,
) -> IngestionReport:
    try:
        data_source = catalog.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        return offline_import.import_manual(
            tenant_id,
            data_source_id,
            _manual_snapshot(data_source_id, data_source.dialect, payload),
        )
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (CatalogIngestionError, OfflineImportModeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/schema",
    response_model=SchemaExplorerView,
)
def explore_schema(
    tenant_id: str,
    data_source_id: str,
    explorer: SchemaExplorerDependency,
) -> SchemaExplorerSnapshot:
    try:
        return explorer.get_latest(tenant_id, data_source_id)
    except (DataSourceNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/semantic-reviews",
    response_model=tuple[SemanticReviewView, ...],
)
def list_semantic_reviews(
    tenant_id: str,
    data_source_id: str,
    governance: SemanticGovernanceDependency,
) -> tuple[SemanticReviewItem, ...]:
    try:
        return governance.list_review_queue(tenant_id, data_source_id)
    except (DataSourceNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/semantics/history",
    response_model=tuple[SemanticEvidenceView, ...],
)
def get_semantic_history(
    tenant_id: str,
    data_source_id: str,
    governance: SemanticGovernanceDependency,
    object_ref: Annotated[str, Query(min_length=3, max_length=700)],
) -> tuple[SemanticEvidenceEntry, ...]:
    try:
        return governance.history(tenant_id, data_source_id, object_ref)
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        SemanticObjectNotFoundError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/semantics/corrections",
    response_model=SemanticCorrectionResultView,
    status_code=status.HTTP_201_CREATED,
)
def correct_semantics(
    tenant_id: str,
    data_source_id: str,
    payload: SemanticCorrectionCreate,
    governance: SemanticGovernanceDependency,
    actor: SemanticManagerDependency,
) -> SemanticCorrectionResult:
    try:
        return governance.correct(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            object_ref=payload.object_ref,
            actor_id=actor.actor_id,
            description=payload.description,
            reason=payload.reason,
            expected_updated_at=payload.expected_updated_at,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        SemanticObjectNotFoundError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SemanticConcurrencyError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (SemanticDescriptionRequiredError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-concepts/proposals",
    response_model=BusinessConceptWriteView,
    status_code=status.HTTP_201_CREATED,
)
def propose_business_concept(
    tenant_id: str,
    data_source_id: str,
    payload: BusinessConceptProposalCreate,
    concepts: BusinessConceptDependency,
    actor: SemanticManagerDependency,
) -> BusinessConceptWriteResult:
    try:
        return concepts.propose(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            concept_key=payload.concept_key,
            name=payload.name,
            description=payload.description,
            synonyms=payload.synonyms,
            object_refs=payload.object_refs,
            content_classification=payload.content_classification,
            status=payload.status,
            source=payload.source,
            confidence=payload.confidence,
            actor_id=actor.actor_id,
            reason=payload.reason,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        BusinessConceptObjectNotFoundError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-concepts/corrections",
    response_model=BusinessConceptCorrectionResultView,
    status_code=status.HTTP_201_CREATED,
)
def correct_business_concept(
    tenant_id: str,
    data_source_id: str,
    payload: BusinessConceptCorrectionCreate,
    concepts: BusinessConceptDependency,
    actor: SemanticManagerDependency,
) -> BusinessConceptCorrectionResult:
    try:
        return concepts.correct(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            concept_key=payload.concept_key,
            name=payload.name,
            description=payload.description,
            synonyms=payload.synonyms,
            object_refs=payload.object_refs,
            content_classification=payload.content_classification,
            actor_id=actor.actor_id,
            reason=payload.reason,
            expected_updated_at=payload.expected_updated_at,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        BusinessConceptObjectNotFoundError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (BusinessConceptConcurrencyError, BusinessTermConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-concepts",
    response_model=tuple[BusinessConceptResolutionView, ...],
)
def list_business_concepts(
    tenant_id: str,
    data_source_id: str,
    concepts: BusinessConceptDependency,
) -> tuple[BusinessConceptResolution, ...]:
    try:
        return concepts.list_concepts(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "business-concepts/{concept_key}/history"
    ),
    response_model=tuple[BusinessConceptEvidenceView, ...],
)
def get_business_concept_history(
    tenant_id: str,
    data_source_id: str,
    concept_key: str,
    concepts: BusinessConceptDependency,
) -> tuple[BusinessConceptEvidenceEntry, ...]:
    try:
        return concepts.history(tenant_id, data_source_id, concept_key)
    except (DataSourceNotFoundError, BusinessConceptNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-concept-reviews",
    response_model=tuple[BusinessConceptReviewView, ...],
)
def list_business_concept_reviews(
    tenant_id: str,
    data_source_id: str,
    concepts: BusinessConceptDependency,
) -> tuple[BusinessConceptReviewItem, ...]:
    try:
        return concepts.list_review_queue(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-terms/resolution",
    response_model=BusinessTermResolutionView,
)
def resolve_business_terms(
    tenant_id: str,
    data_source_id: str,
    payload: BusinessTermResolutionCreate,
    concepts: BusinessConceptDependency,
) -> BusinessTermResolution:
    try:
        return concepts.resolve_terms(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            query=payload.query,
        )
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/metric-definitions/proposals",
    response_model=MetricWriteView,
    status_code=status.HTTP_201_CREATED,
)
def propose_metric_definition(
    tenant_id: str,
    data_source_id: str,
    payload: MetricProposalCreate,
    semantics: AnalyticsSemanticsDependency,
    actor: SemanticManagerDependency,
) -> AnalyticSemanticWriteResult:
    try:
        return semantics.propose_metric(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            metric_key=payload.metric_key,
            name=payload.name,
            description=payload.description,
            expression_sql=payload.expression_sql,
            grain_refs=payload.grain_refs,
            dimension_refs=payload.dimension_refs,
            concept_keys=payload.concept_keys,
            rule_keys=payload.rule_keys,
            content_classification=payload.content_classification,
            status=payload.status,
            source=payload.source,
            confidence=payload.confidence,
            actor_id=actor.actor_id,
            reason=payload.reason,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        AnalyticSemanticReferenceError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (AnalyticSemanticValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/metric-definitions/corrections",
    response_model=MetricWriteView,
    status_code=status.HTTP_201_CREATED,
)
def correct_metric_definition(
    tenant_id: str,
    data_source_id: str,
    payload: MetricCorrectionCreate,
    semantics: AnalyticsSemanticsDependency,
    actor: SemanticManagerDependency,
) -> AnalyticSemanticWriteResult:
    try:
        return semantics.correct_metric(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            metric_key=payload.metric_key,
            name=payload.name,
            description=payload.description,
            expression_sql=payload.expression_sql,
            grain_refs=payload.grain_refs,
            dimension_refs=payload.dimension_refs,
            concept_keys=payload.concept_keys,
            rule_keys=payload.rule_keys,
            content_classification=payload.content_classification,
            actor_id=actor.actor_id,
            reason=payload.reason,
            expected_updated_at=payload.expected_updated_at,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        AnalyticSemanticReferenceError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (
        AnalyticSemanticConcurrencyError,
        AnalyticSemanticNameConflictError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (AnalyticSemanticValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/metric-definitions",
    response_model=tuple[MetricResolutionView, ...],
)
def list_metric_definitions(
    tenant_id: str,
    data_source_id: str,
    semantics: AnalyticsSemanticsDependency,
) -> tuple[MetricResolution, ...]:
    try:
        return semantics.list_metrics(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "metric-definitions/{metric_key}/history"
    ),
    response_model=tuple[AnalyticSemanticEvidenceView, ...],
)
def get_metric_definition_history(
    tenant_id: str,
    data_source_id: str,
    metric_key: str,
    semantics: AnalyticsSemanticsDependency,
) -> tuple[AnalyticSemanticEvidenceEntry, ...]:
    try:
        return semantics.history(
            tenant_id,
            data_source_id,
            AnalyticSemanticKind.METRIC,
            metric_key,
        )
    except (DataSourceNotFoundError, AnalyticSemanticNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-rules/proposals",
    response_model=BusinessRuleWriteView,
    status_code=status.HTTP_201_CREATED,
)
def propose_business_rule(
    tenant_id: str,
    data_source_id: str,
    payload: BusinessRuleProposalCreate,
    semantics: AnalyticsSemanticsDependency,
    actor: SemanticManagerDependency,
) -> AnalyticSemanticWriteResult:
    try:
        return semantics.propose_business_rule(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            rule_key=payload.rule_key,
            name=payload.name,
            description=payload.description,
            predicate_sql=payload.predicate_sql,
            concept_keys=payload.concept_keys,
            content_classification=payload.content_classification,
            status=payload.status,
            source=payload.source,
            confidence=payload.confidence,
            actor_id=actor.actor_id,
            reason=payload.reason,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        AnalyticSemanticReferenceError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (AnalyticSemanticValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-rules/corrections",
    response_model=BusinessRuleWriteView,
    status_code=status.HTTP_201_CREATED,
)
def correct_business_rule(
    tenant_id: str,
    data_source_id: str,
    payload: BusinessRuleCorrectionCreate,
    semantics: AnalyticsSemanticsDependency,
    actor: SemanticManagerDependency,
) -> AnalyticSemanticWriteResult:
    try:
        return semantics.correct_business_rule(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            rule_key=payload.rule_key,
            name=payload.name,
            description=payload.description,
            predicate_sql=payload.predicate_sql,
            concept_keys=payload.concept_keys,
            content_classification=payload.content_classification,
            actor_id=actor.actor_id,
            reason=payload.reason,
            expected_updated_at=payload.expected_updated_at,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        AnalyticSemanticReferenceError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (
        AnalyticSemanticConcurrencyError,
        AnalyticSemanticNameConflictError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (AnalyticSemanticValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/business-rules",
    response_model=tuple[BusinessRuleResolutionView, ...],
)
def list_business_rules(
    tenant_id: str,
    data_source_id: str,
    semantics: AnalyticsSemanticsDependency,
) -> tuple[BusinessRuleResolution, ...]:
    try:
        return semantics.list_business_rules(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "business-rules/{rule_key}/history"
    ),
    response_model=tuple[AnalyticSemanticEvidenceView, ...],
)
def get_business_rule_history(
    tenant_id: str,
    data_source_id: str,
    rule_key: str,
    semantics: AnalyticsSemanticsDependency,
) -> tuple[AnalyticSemanticEvidenceEntry, ...]:
    try:
        return semantics.history(
            tenant_id,
            data_source_id,
            AnalyticSemanticKind.BUSINESS_RULE,
            rule_key,
        )
    except (DataSourceNotFoundError, AnalyticSemanticNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/analytic-semantic-reviews",
    response_model=tuple[AnalyticSemanticReviewView, ...],
)
def list_analytic_semantic_reviews(
    tenant_id: str,
    data_source_id: str,
    semantics: AnalyticsSemanticsDependency,
) -> tuple[AnalyticSemanticReviewItem, ...]:
    try:
        return semantics.list_review_queue(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "learning/sql-examples"
    ),
    response_model=CorrectedSQLExampleEntryView,
    status_code=status.HTTP_201_CREATED,
)
def record_corrected_sql_example(
    tenant_id: str,
    data_source_id: str,
    payload: CorrectedSQLExampleCreate,
    learning_loop: LearningLoopDependency,
    actor: FeedbackWriterDependency,
) -> CorrectedSQLExampleEntry:
    try:
        return learning_loop.correct(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            question=payload.question,
            corrected_sql=payload.corrected_sql,
            actor_id=actor.actor_id,
            content_classification=payload.content_classification,
            business_concepts=payload.business_concepts,
            assumptions=payload.assumptions,
            reason=payload.reason,
            source_query_request_id=payload.source_query_request_id,
            supersedes_example_id=payload.supersedes_example_id,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        CorrectedSQLSourceNotFoundError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except CorrectedSQLConcurrencyError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (CorrectedSQLValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "learning/sql-examples"
    ),
    response_model=tuple[CorrectedSQLExampleEntryView, ...],
)
def list_corrected_sql_examples(
    tenant_id: str,
    data_source_id: str,
    learning_loop: LearningLoopDependency,
    include_superseded: bool = False,
) -> tuple[CorrectedSQLExampleEntry, ...]:
    try:
        return learning_loop.list_examples(
            tenant_id,
            data_source_id,
            include_superseded=include_superseded,
        )
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "learning/sql-examples/retrieval"
    ),
    response_model=tuple[SQLExampleMatchView, ...],
)
def retrieve_corrected_sql_examples(
    tenant_id: str,
    data_source_id: str,
    payload: SQLExampleRetrievalCreate,
    learning_loop: LearningLoopDependency,
) -> tuple[SQLExampleMatch, ...]:
    try:
        return learning_loop.retrieve(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            question=payload.question,
            max_results=payload.max_results,
        )
    except (DataSourceNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "query-requests/{query_request_id}/feedback"
    ),
    response_model=QueryFeedbackView,
    status_code=status.HTTP_201_CREATED,
)
def record_query_feedback(
    tenant_id: str,
    data_source_id: str,
    query_request_id: str,
    payload: QueryFeedbackCreate,
    governance: LearningGovernanceDependency,
    actor: FeedbackWriterDependency,
) -> QueryFeedbackEvent:
    try:
        return governance.record_feedback(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            query_request_id=query_request_id,
            outcome=payload.outcome,
            actor_id=actor.actor_id,
            reason=payload.reason,
            corrected_sql_example_id=payload.corrected_sql_example_id,
        )
    except (DataSourceNotFoundError, FeedbackLinkNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except FeedbackConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (FeedbackNotEligibleError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "feedback/summary"
    ),
    response_model=FeedbackSummaryView,
)
def get_feedback_summary(
    tenant_id: str,
    data_source_id: str,
    governance: LearningGovernanceDependency,
) -> FeedbackSummary:
    try:
        return governance.summarize_feedback(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "learning/golden-candidates"
    ),
    response_model=GoldenCandidateEntryView,
    status_code=status.HTTP_201_CREATED,
)
def promote_golden_candidate(
    tenant_id: str,
    data_source_id: str,
    payload: GoldenCandidateCreate,
    governance: LearningGovernanceDependency,
    _actor: GoldenReviewerDependency,
) -> GoldenCandidateEntry:
    try:
        return governance.promote_candidate(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            corrected_sql_example_id=payload.corrected_sql_example_id,
        )
    except (
        DataSourceNotFoundError,
        CatalogNotIngestedError,
        FeedbackLinkNotFoundError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GoldenCandidateConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except GoldenCandidateEligibilityError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "learning/golden-candidates"
    ),
    response_model=tuple[GoldenCandidateEntryView, ...],
)
def list_golden_candidates(
    tenant_id: str,
    data_source_id: str,
    governance: LearningGovernanceDependency,
    candidate_status: GoldenCandidateStatus | None = None,
) -> tuple[GoldenCandidateEntry, ...]:
    try:
        return governance.list_candidates(
            tenant_id,
            data_source_id,
            candidate_status=candidate_status,
        )
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "learning/golden-candidates/export"
    ),
    response_model=GoldenCandidateExportView,
)
def export_golden_candidates(
    tenant_id: str,
    data_source_id: str,
    governance: LearningGovernanceDependency,
) -> GoldenCandidateExport:
    try:
        return governance.export_approved(tenant_id, data_source_id)
    except DataSourceNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "learning/golden-candidates/{candidate_id}/reviews"
    ),
    response_model=GoldenCandidateEntryView,
    status_code=status.HTTP_201_CREATED,
)
def review_golden_candidate(
    tenant_id: str,
    data_source_id: str,
    candidate_id: str,
    payload: GoldenCandidateReviewCreate,
    governance: LearningGovernanceDependency,
    actor: GoldenReviewerDependency,
) -> GoldenCandidateEntry:
    try:
        return governance.review_candidate(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            candidate_id=candidate_id,
            decision=payload.decision,
            actor_id=actor.actor_id,
            reason=payload.reason,
        )
    except GoldenCandidateNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GoldenCandidateConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "semantics/inference-jobs"
    ),
    response_model=BackgroundJobView,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_semantic_inference_job(
    tenant_id: str,
    data_source_id: str,
    payload: SemanticInferenceJobCreate,
    request: Request,
    catalog: CatalogDependency,
    _actor: SemanticManagerDependency,
) -> BackgroundJob:
    provider_ids = cast(tuple[str, ...], request.app.state.llm_provider_ids)
    if payload.provider_id not in provider_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provider is not configured",
        )
    try:
        return catalog.enqueue_background_job(
            BackgroundJob(
                tenant_id=tenant_id,
                data_source_id=data_source_id,
                job_type="semantic_inference",
                payload={"provider_id": payload.provider_id},
                max_attempts=payload.max_attempts,
            )
        )
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/background-jobs",
    response_model=tuple[BackgroundJobView, ...],
)
def list_background_jobs(
    tenant_id: str,
    catalog: CatalogDependency,
    _actor: SemanticManagerDependency,
    data_source_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> tuple[BackgroundJob, ...]:
    try:
        return catalog.list_background_jobs(
            tenant_id,
            data_source_id=data_source_id,
            limit=limit,
        )
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.get(
    "/v1/tenants/{tenant_id}/background-jobs/{job_id}",
    response_model=BackgroundJobView,
)
def get_background_job(
    tenant_id: str,
    job_id: str,
    catalog: CatalogDependency,
    _actor: SemanticManagerDependency,
) -> BackgroundJob:
    job = catalog.get_background_job(tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@app.delete(
    "/v1/tenants/{tenant_id}/background-jobs/{job_id}",
    response_model=BackgroundJobView,
)
def cancel_background_job(
    tenant_id: str,
    job_id: str,
    catalog: CatalogDependency,
    _actor: SemanticManagerDependency,
) -> BackgroundJob:
    try:
        return catalog.cancel_background_job(tenant_id, job_id, now=utc_now())
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/semantics/inferences",
    response_model=SemanticInferenceRunView,
    status_code=status.HTTP_201_CREATED,
)
def infer_semantics(
    tenant_id: str,
    data_source_id: str,
    payload: SemanticInferenceCreate,
    inference: SemanticInferenceDependency,
    _actor: SemanticManagerDependency,
) -> SemanticInferenceRun:
    try:
        return inference.infer_missing_descriptions(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            provider_id=payload.provider_id,
        )
    except (DataSourceNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except PromptEgressBlockedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=dict(error.safe_detail()),
        ) from error
    except LLMBudgetExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "budget_exceeded",
                "message": str(error),
                "provider_invoked": False,
                "provider_id": payload.provider_id,
                "purpose": "semantic_description_inference",
                "next_actions": ("review_budget", "select_lower_cost_model"),
            },
        ) from error
    except (
        SemanticInferenceNoTargetsError,
        InvalidSemanticInferenceOutputError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (
        LLMProviderNotFoundError,
        LLMProviderCapabilityError,
        LLMProviderCallError,
    ) as error:
        provider_invoked = isinstance(error, LLMProviderCallError)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "provider_unavailable",
                "message": str(error),
                "provider_invoked": provider_invoked,
                "provider_id": payload.provider_id,
                "purpose": "semantic_description_inference",
                "next_actions": ("retry_later",),
            },
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/context/previews",
    response_model=SchemaContextView,
)
def preview_context(
    tenant_id: str,
    data_source_id: str,
    payload: ContextPreviewCreate,
    context_builder: ContextBuilderDependency,
    _actor: QueryUserDependency,
) -> SchemaContextSnapshot:
    try:
        return context_builder.build(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            query=payload.query,
            max_seed_objects=payload.max_seed_objects,
            max_objects=payload.max_objects,
            graph_hops=payload.graph_hops,
            target_columns_per_object=payload.target_columns_per_object,
            max_sql_examples=payload.max_sql_examples,
        )
    except (DataSourceNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (ContextNoMatchesError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/sql/preflights",
    response_model=SQLGenerationPreflightView,
    status_code=status.HTTP_201_CREATED,
)
def preflight_sql_proposal(
    tenant_id: str,
    data_source_id: str,
    payload: SQLGenerationPreflightCreate,
    generation: SQLGenerationDependency,
    request: Request,
    actor: QueryUserDependency,
) -> SQLGenerationPreflight:
    try:
        classification = cast(
            ServerSideTextClassifier,
            request.app.state.text_classifier,
        ).classify(payload.query, payload.question_classification)
        return generation.preflight(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            provider_id=payload.provider_id,
            question=payload.query,
            question_classification=classification.effective,
            declared_classification=classification.declared,
            detected_classification=classification.detected,
            detection_reason_codes=classification.reasons,
            actor_id=actor.actor_id,
            max_seed_objects=payload.max_seed_objects,
            max_objects=payload.max_objects,
            graph_hops=payload.graph_hops,
            target_columns_per_object=payload.target_columns_per_object,
            max_sql_examples=payload.max_sql_examples,
            privacy_mode=payload.privacy_mode,
            force_semantic=payload.force_semantic,
        )
    except (DataSourceNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (ContextNoMatchesError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (LLMProviderNotFoundError, LLMProviderCapabilityError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "provider_unavailable",
                "message": str(error),
                "provider_invoked": False,
                "provider_id": payload.provider_id,
                "purpose": "sql_proposal_generation",
            },
        ) from error


@app.post(
    "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/sql/proposals",
    response_model=SQLGenerationRunView,
    status_code=status.HTTP_201_CREATED,
)
def generate_sql_proposal(
    tenant_id: str,
    data_source_id: str,
    payload: SQLGenerationCreate,
    generation: SQLGenerationDependency,
    request: Request,
    actor: QueryUserDependency,
) -> SQLGenerationRun:
    try:
        classification = cast(
            ServerSideTextClassifier,
            request.app.state.text_classifier,
        ).classify(payload.query, payload.question_classification)
        return generation.generate(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            provider_id=payload.provider_id,
            question=payload.query,
            question_classification=classification.effective,
            declared_classification=classification.declared,
            detected_classification=classification.detected,
            detection_reason_codes=classification.reasons,
            actor_id=actor.actor_id,
            max_seed_objects=payload.max_seed_objects,
            max_objects=payload.max_objects,
            graph_hops=payload.graph_hops,
            target_columns_per_object=payload.target_columns_per_object,
            max_sql_examples=payload.max_sql_examples,
            privacy_mode=payload.privacy_mode,
            force_semantic=payload.force_semantic,
            confirmation_token=payload.confirmation_token,
        )
    except (DataSourceNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except PromptEgressBlockedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=dict(error.safe_detail()),
        ) from error
    except PreflightConfirmationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": error.code,
                "message": str(error),
                "provider_invoked": False,
                "provider_id": payload.provider_id,
                "purpose": "sql_proposal_generation",
                "next_actions": ("run_new_preflight",),
            },
        ) from error
    except LLMBudgetExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "budget_exceeded",
                "message": str(error),
                "provider_invoked": False,
                "provider_id": payload.provider_id,
                "purpose": "sql_proposal_generation",
                "next_actions": ("review_budget", "select_lower_cost_model"),
            },
        ) from error
    except (ContextNoMatchesError, InvalidSQLProposalOutputError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (
        LLMProviderNotFoundError,
        LLMProviderCapabilityError,
        LLMProviderCallError,
    ) as error:
        provider_invoked = isinstance(error, LLMProviderCallError)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "provider_unavailable",
                "message": str(error),
                "provider_invoked": provider_invoked,
                "provider_id": payload.provider_id,
                "purpose": "sql_proposal_generation",
                "next_actions": ("retry_later",),
            },
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "query-requests/{query_request_id}/intent-corrections"
    ),
    response_model=IntentMemoryCorrectionResultView,
    status_code=status.HTTP_201_CREATED,
)
def correct_intent_memory(
    tenant_id: str,
    data_source_id: str,
    query_request_id: str,
    payload: IntentMemoryCorrectionCreate,
    memory: IntentMemoryDependency,
    actor: SemanticManagerDependency,
) -> IntentMemoryCorrectionResult:
    try:
        return memory.correct_mapping(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            query_request_id=query_request_id,
            term=payload.term,
            role=payload.role,
            corrected_object_ref=payload.corrected_object_ref,
            previous_object_ref=payload.previous_object_ref,
            actor_id=actor.actor_id,
            reason=payload.reason,
        )
    except (IntentMemoryQueryNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (
        IntentMemoryStaleCatalogError,
        IntentMemoryTermConflictError,
        BusinessConceptConcurrencyError,
        BusinessTermConflictError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (IntentMemoryReferenceError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "query-requests/{query_request_id}/intent-corrections/from-text"
    ),
    response_model=IntentCorrectionRunView,
    status_code=status.HTTP_201_CREATED,
)
def correct_intent_memory_from_text(
    tenant_id: str,
    data_source_id: str,
    query_request_id: str,
    payload: FreeTextIntentCorrectionCreate,
    interpreter: IntentCorrectionInterpreterDependency,
    request: Request,
    actor: SemanticManagerDependency,
) -> IntentCorrectionRun:
    try:
        classification = cast(
            ServerSideTextClassifier,
            request.app.state.text_classifier,
        ).classify(payload.correction_text, payload.correction_classification)
        return interpreter.interpret_and_apply(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            query_request_id=query_request_id,
            provider_id=payload.provider_id,
            correction_text=payload.correction_text,
            correction_classification=classification.effective,
            current_entities=tuple(
                CurrentIntentEntity(
                    term=entity.term,
                    role=entity.role,
                    object_ref=entity.object_ref,
                )
                for entity in payload.current_entities
            ),
            actor_id=actor.actor_id,
        )
    except (IntentMemoryQueryNotFoundError, CatalogNotIngestedError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except PromptEgressBlockedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=dict(error.safe_detail()),
        ) from error
    except LLMBudgetExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(error),
        ) from error
    except (
        IntentMemoryStaleCatalogError,
        IntentMemoryTermConflictError,
        BusinessConceptConcurrencyError,
        BusinessTermConflictError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (
        IntentMemoryReferenceError,
        InvalidIntentCorrectionOutputError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (
        LLMProviderNotFoundError,
        LLMProviderCapabilityError,
        LLMProviderCallError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "query-requests/{request_id}/explain"
    ),
    response_model=ExplainResultView,
)
def explain_query_request(
    tenant_id: str,
    data_source_id: str,
    request_id: str,
    execution: QueryExecutionDependency,
    _actor: QueryUserDependency,
    payload: QueryParameterBindings | None = None,
) -> ExplainResult:
    try:
        return execution.explain(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            request_id=request_id,
            parameters=payload.parameters if payload is not None else None,
        )
    except QueryExecutionNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except QueryExecutionPolicyBlockedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except (QueryExecutionStateError, QueryExecutionStaleError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (
        QueryExecutionValidationError,
        ReadOnlyExecutorConfigurationError,
        SecretResolutionError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (
        QueryExecutionUnavailableError,
        ReadOnlyExecutorUnavailableError,
        ReadOnlyExecutionError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "query-requests/{request_id}/approval"
    ),
    response_model=QueryRequestStateView,
)
def approve_query_request(
    tenant_id: str,
    data_source_id: str,
    request_id: str,
    payload: QueryApprovalCreate,
    execution: QueryExecutionDependency,
    actor: QueryApproverDependency,
) -> QueryRequest:
    try:
        return execution.approve(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            request_id=request_id,
            actor_id=actor.actor_id,
            parameters=payload.parameters,
        )
    except QueryExecutionNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except QueryExecutionPolicyBlockedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except QueryExecutionUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (
        QueryExecutionStateError,
        QueryExecutionStaleError,
        InvalidStateTransition,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (QueryExecutionValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "query-requests/{request_id}/executions"
    ),
    response_model=QueryExecutionRunView,
)
def execute_query_request(
    tenant_id: str,
    data_source_id: str,
    request_id: str,
    execution: QueryExecutionDependency,
    _actor: QueryUserDependency,
    payload: QueryParameterBindings | None = None,
) -> QueryExecutionRun:
    try:
        return execution.execute(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            request_id=request_id,
            parameters=payload.parameters if payload is not None else None,
        )
    except QueryExecutionNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except QueryExecutionPolicyBlockedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except (
        QueryExecutionStateError,
        QueryExecutionStaleError,
        InvalidStateTransition,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (
        QueryExecutionValidationError,
        ReadOnlyExecutorConfigurationError,
        SecretResolutionError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (
        QueryExecutionUnavailableError,
        ReadOnlyExecutorUnavailableError,
        ReadOnlyExecutionError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@app.post(
    (
        "/v1/tenants/{tenant_id}/data-sources/{data_source_id}/"
        "query-requests/{request_id}/cancellation"
    ),
    response_model=QueryRequestStateView,
)
def cancel_query_request(
    tenant_id: str,
    data_source_id: str,
    request_id: str,
    execution: QueryExecutionDependency,
    _actor: QueryUserDependency,
) -> QueryRequest:
    try:
        return execution.cancel(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            request_id=request_id,
        )
    except QueryExecutionNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (
        QueryExecutionStateError,
        QueryExecutionUnavailableError,
        InvalidStateTransition,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


def _manual_snapshot(
    data_source_id: str,
    dialect: str,
    payload: ManualImportCreate,
) -> DataSourceSnapshot:
    return DataSourceSnapshot(
        data_source_id=data_source_id,
        dialect=dialect,
        objects=tuple(
            SchemaObjectSnapshot(
                schema_name=schema_object.schema_name,
                name=schema_object.name,
                kind=schema_object.kind,
                columns=tuple(
                    ColumnSnapshot(
                        name=column.name,
                        physical_type=column.physical_type,
                        ordinal=column.ordinal,
                        nullable=column.nullable,
                        default_expression=column.default_expression,
                        is_primary_key=column.is_primary_key,
                        comment=column.comment,
                        classification=column.classification,
                    )
                    for column in schema_object.columns
                ),
                definition_sql=schema_object.definition_sql,
                comment=schema_object.comment,
            )
            for schema_object in payload.objects
        ),
        relationships=tuple(
            RelationshipSnapshot(
                name=relationship.name,
                source_object_ref=relationship.source_object_ref,
                target_object_ref=relationship.target_object_ref,
                source_columns=relationship.source_columns,
                target_columns=relationship.target_columns,
            )
            for relationship in payload.relationships
        ),
    )
