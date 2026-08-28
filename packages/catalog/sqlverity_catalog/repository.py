from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from importlib import import_module
from pathlib import Path
from threading import RLock
from typing import Any, Concatenate, cast
from uuid import UUID, uuid4

from packages.domain.sqlverity_domain.epistemic import (
    ResolutionAction,
    ResolutionDecision,
    resolve_business_concept_update,
    resolve_business_rule_update,
    resolve_metric_update,
    resolve_semantic_update,
)
from packages.domain.sqlverity_domain.models import (
    AIContentManifestCount,
    AITransferReceipt,
    AnalyticSemanticKind,
    APICredential,
    APICredentialRevocation,
    AuditEvent,
    AuthorizedQueryDefinition,
    AuthorizedQueryParameter,
    BackgroundJob,
    BackgroundJobStatus,
    BudgetPeriod,
    BusinessConceptDefinition,
    BusinessConceptResolution,
    BusinessRuleDefinition,
    BusinessRuleResolution,
    CatalogVersion,
    Classification,
    ColumnDefinition,
    CorrectedSQLExample,
    DataSource,
    DataSourceCapability,
    DataSourceRoleAssignment,
    DataSourceType,
    EpistemicStatus,
    ExecutionCostPolicy,
    GoldenCandidateReview,
    GoldenCandidateStatus,
    GoldenEvaluationCandidate,
    LLMUsageEvent,
    MetricDefinition,
    MetricResolution,
    ModelPricing,
    ObjectKind,
    OutputColumnLineage,
    PlatformRole,
    ProviderEgressPolicy,
    ProviderRetentionMode,
    QueryFeedbackEvent,
    QueryFeedbackOutcome,
    QueryParameterDefinition,
    QueryParameterType,
    QueryRequest,
    QueryRequestState,
    Relationship,
    SchemaObject,
    SecurityPrincipal,
    SemanticDefinition,
    SemanticResolution,
    Tenant,
    TenantBudget,
    TenantRoleAssignment,
    utc_now,
)
from packages.domain.sqlverity_domain.query_state import QueryLifecycle


def _locked[**P, R](
    method: Callable[Concatenate[Any, P], R],
) -> Callable[Concatenate[Any, P], R]:
    @wraps(method)
    def wrapper(
        self: Any,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(Callable[Concatenate[Any, P], R], wrapper)


@dataclass(frozen=True, slots=True)
class SemanticWriteResult:
    evidence: SemanticDefinition
    resolution: SemanticResolution
    action: ResolutionAction


@dataclass(frozen=True, slots=True)
class BusinessConceptWriteResult:
    evidence: BusinessConceptDefinition
    resolution: BusinessConceptResolution
    action: ResolutionAction


type AnalyticSemanticDefinition = MetricDefinition | BusinessRuleDefinition
type AnalyticSemanticResolution = MetricResolution | BusinessRuleResolution


@dataclass(frozen=True, slots=True)
class AnalyticSemanticWriteResult:
    evidence: AnalyticSemanticDefinition
    resolution: AnalyticSemanticResolution
    action: ResolutionAction


@dataclass(frozen=True, slots=True)
class OperationalRetentionReport:
    cutoff: datetime
    background_jobs: int
    quota_windows: int
    run_id: str | None = None
    actor_id: str | None = None
    completed_at: datetime | None = None


class SemanticResolutionConflictError(RuntimeError):
    pass


class BusinessConceptResolutionConflictError(RuntimeError):
    pass


class AnalyticSemanticResolutionConflictError(RuntimeError):
    pass


class CorrectedSQLExampleConflictError(RuntimeError):
    pass


class LearningGovernanceConflictError(RuntimeError):
    pass


class SecurityConflictError(RuntimeError):
    pass


class SQLiteCatalogRepository:
    """Local catalog adapter with tenant-scoped reads and immutable evidence/audit rows."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    @_locked
    def initialize(self) -> None:
        schema = Path(__file__).with_name("sqlite_schema.sql").read_text(encoding="utf-8")
        self._connection.executescript(schema)
        self._upgrade_legacy_schema()

    @_locked
    def close(self) -> None:
        self._connection.close()

    @_locked
    def health_check(self) -> bool:
        row = self._connection.execute("SELECT 1 AS healthy").fetchone()
        return row is not None and int(row["healthy"]) == 1

    @_locked
    def create_tenant(self, name: str) -> Tenant:
        tenant = Tenant(name=name.strip())
        with self._connection:
            self._connection.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                (tenant.id, tenant.name, tenant.created_at.isoformat()),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant.id,
                    event_type="tenant.created",
                    subject_type="tenant",
                    subject_id=tenant.id,
                )
            )
        return tenant

    @_locked
    def get_tenant(self, tenant_id: str) -> Tenant | None:
        row = self._connection.execute(
            "SELECT * FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            return None
        return _tenant_from_row(row)

    @_locked
    def list_tenants(self) -> tuple[Tenant, ...]:
        rows = self._connection.execute(
            "SELECT * FROM tenants ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
        return tuple(_tenant_from_row(row) for row in rows)

    @_locked
    def create_security_access(
        self,
        principal: SecurityPrincipal,
        credential: APICredential,
        *,
        tenant_assignment: TenantRoleAssignment | None,
        data_source_assignments: tuple[DataSourceRoleAssignment, ...],
    ) -> None:
        if (tenant_assignment is None) == (not data_source_assignments):
            raise ValueError(
                "Security access requires either one tenant role or DataSource roles"
            )
        if credential.principal_id != principal.id:
            raise ValueError("API credential belongs to another principal")
        assignments: tuple[TenantRoleAssignment | DataSourceRoleAssignment, ...]
        assignments = (
            (tenant_assignment,)
            if tenant_assignment is not None
            else data_source_assignments
        )
        if any(
            assignment.tenant_id != principal.tenant_id
            or assignment.principal_id != principal.id
            for assignment in assignments
        ):
            raise ValueError("Security assignment scope does not match the principal")
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO security_principals
                        (id, tenant_id, subject, display_name, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        principal.id,
                        principal.tenant_id,
                        principal.subject,
                        principal.display_name,
                        principal.created_at.isoformat(),
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO api_credentials
                        (id, tenant_id, principal_id, label, token_sha256,
                         expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        credential.id,
                        credential.tenant_id,
                        credential.principal_id,
                        credential.label,
                        credential.token_sha256,
                        (
                            credential.expires_at.isoformat()
                            if credential.expires_at is not None
                            else None
                        ),
                        credential.created_at.isoformat(),
                    ),
                )
                if tenant_assignment is not None:
                    self._connection.execute(
                        """
                        INSERT INTO tenant_role_assignments
                            (id, tenant_id, principal_id, role, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tenant_assignment.id,
                            tenant_assignment.tenant_id,
                            tenant_assignment.principal_id,
                            tenant_assignment.role.value,
                            tenant_assignment.created_by,
                            tenant_assignment.created_at.isoformat(),
                        ),
                    )
                else:
                    self._connection.executemany(
                        """
                        INSERT INTO data_source_role_assignments
                            (id, tenant_id, data_source_id, principal_id, role,
                             created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                assignment.id,
                                assignment.tenant_id,
                                assignment.data_source_id,
                                assignment.principal_id,
                                assignment.role.value,
                                assignment.created_by,
                                assignment.created_at.isoformat(),
                            )
                            for assignment in data_source_assignments
                        ),
                    )
                self._append_audit(
                    AuditEvent(
                        tenant_id=principal.tenant_id,
                        event_type="security.principal_provisioned",
                        subject_type="security_principal",
                        subject_id=principal.id,
                        details={
                            "credential_id": credential.id,
                            "role": assignments[0].role.value,
                            "scope": (
                                "tenant" if tenant_assignment is not None else "data_source"
                            ),
                            "data_source_count": len(data_source_assignments),
                            "created_by": assignments[0].created_by,
                        },
                    )
                )
        except sqlite3.IntegrityError as error:
            raise SecurityConflictError(
                "Security subject, credential, or role assignment already exists or is invalid"
            ) from error

    @_locked
    def create_federated_security_access(
        self,
        principal: SecurityPrincipal,
        *,
        tenant_assignment: TenantRoleAssignment | None,
        data_source_assignments: tuple[DataSourceRoleAssignment, ...],
    ) -> None:
        if (tenant_assignment is None) == (not data_source_assignments):
            raise ValueError(
                "Federated access requires either one tenant role or DataSource roles"
            )
        assignments: tuple[TenantRoleAssignment | DataSourceRoleAssignment, ...]
        assignments = (
            (tenant_assignment,)
            if tenant_assignment is not None
            else data_source_assignments
        )
        if any(
            assignment.tenant_id != principal.tenant_id
            or assignment.principal_id != principal.id
            for assignment in assignments
        ):
            raise ValueError("Security assignment scope does not match the principal")
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO security_principals
                        (id, tenant_id, subject, display_name, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        principal.id,
                        principal.tenant_id,
                        principal.subject,
                        principal.display_name,
                        principal.created_at.isoformat(),
                    ),
                )
                if tenant_assignment is not None:
                    self._connection.execute(
                        """
                        INSERT INTO tenant_role_assignments
                            (id, tenant_id, principal_id, role, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tenant_assignment.id,
                            tenant_assignment.tenant_id,
                            tenant_assignment.principal_id,
                            tenant_assignment.role.value,
                            tenant_assignment.created_by,
                            tenant_assignment.created_at.isoformat(),
                        ),
                    )
                else:
                    self._connection.executemany(
                        """
                        INSERT INTO data_source_role_assignments
                            (id, tenant_id, data_source_id, principal_id, role,
                             created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                assignment.id,
                                assignment.tenant_id,
                                assignment.data_source_id,
                                assignment.principal_id,
                                assignment.role.value,
                                assignment.created_by,
                                assignment.created_at.isoformat(),
                            )
                            for assignment in data_source_assignments
                        ),
                    )
                self._append_audit(
                    AuditEvent(
                        tenant_id=principal.tenant_id,
                        event_type="security.federated_principal_provisioned",
                        subject_type="security_principal",
                        subject_id=principal.id,
                        details={
                            "role": assignments[0].role.value,
                            "scope": (
                                "tenant" if tenant_assignment is not None else "data_source"
                            ),
                            "data_source_count": len(data_source_assignments),
                            "created_by": assignments[0].created_by,
                        },
                    )
                )
        except sqlite3.IntegrityError as error:
            raise SecurityConflictError(
                "Federated subject or role assignment already exists or is invalid"
            ) from error

    @_locked
    def get_security_principal(
        self,
        tenant_id: str,
        principal_id: str,
    ) -> SecurityPrincipal | None:
        row = self._connection.execute(
            "SELECT * FROM security_principals WHERE tenant_id = ? AND id = ?",
            (tenant_id, principal_id),
        ).fetchone()
        return _security_principal_from_row(row) if row is not None else None

    @_locked
    def get_security_principal_by_subject(
        self,
        tenant_id: str,
        subject: str,
    ) -> SecurityPrincipal | None:
        row = self._connection.execute(
            """
            SELECT * FROM security_principals
            WHERE tenant_id = ? AND subject = ?
            """,
            (tenant_id, subject),
        ).fetchone()
        return _security_principal_from_row(row) if row is not None else None

    @_locked
    def list_security_principals(
        self,
        tenant_id: str,
    ) -> tuple[SecurityPrincipal, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM security_principals
            WHERE tenant_id = ?
            ORDER BY created_at, id
            """,
            (tenant_id,),
        ).fetchall()
        return tuple(_security_principal_from_row(row) for row in rows)

    @_locked
    def get_api_credential_by_hash(
        self,
        token_sha256: str,
    ) -> APICredential | None:
        row = self._connection.execute(
            "SELECT * FROM api_credentials WHERE token_sha256 = ?",
            (token_sha256,),
        ).fetchone()
        return _api_credential_from_row(row) if row is not None else None

    @_locked
    def get_api_credential(
        self,
        tenant_id: str,
        credential_id: str,
    ) -> APICredential | None:
        row = self._connection.execute(
            "SELECT * FROM api_credentials WHERE tenant_id = ? AND id = ?",
            (tenant_id, credential_id),
        ).fetchone()
        return _api_credential_from_row(row) if row is not None else None

    @_locked
    def list_api_credentials(
        self,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> tuple[APICredential, ...]:
        parameters: tuple[str, ...] = (tenant_id,)
        principal_filter = ""
        if principal_id is not None:
            principal_filter = " AND principal_id = ?"
            parameters = (tenant_id, principal_id)
        rows = self._connection.execute(
            f"""
            SELECT * FROM api_credentials
            WHERE tenant_id = ?{principal_filter}
            ORDER BY created_at, id
            """,
            parameters,
        ).fetchall()
        return tuple(_api_credential_from_row(row) for row in rows)

    @_locked
    def list_tenant_role_assignments(
        self,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> tuple[TenantRoleAssignment, ...]:
        parameters: tuple[str, ...] = (tenant_id,)
        principal_filter = ""
        if principal_id is not None:
            principal_filter = " AND principal_id = ?"
            parameters = (tenant_id, principal_id)
        rows = self._connection.execute(
            f"""
            SELECT * FROM tenant_role_assignments
            WHERE tenant_id = ?{principal_filter}
            ORDER BY created_at, id
            """,
            parameters,
        ).fetchall()
        return tuple(_tenant_role_assignment_from_row(row) for row in rows)

    @_locked
    def list_data_source_role_assignments(
        self,
        tenant_id: str,
        *,
        principal_id: str | None = None,
        data_source_id: str | None = None,
    ) -> tuple[DataSourceRoleAssignment, ...]:
        clauses = ["tenant_id = ?"]
        parameters = [tenant_id]
        if principal_id is not None:
            clauses.append("principal_id = ?")
            parameters.append(principal_id)
        if data_source_id is not None:
            clauses.append("data_source_id = ?")
            parameters.append(data_source_id)
        rows = self._connection.execute(
            f"""
            SELECT * FROM data_source_role_assignments
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at, id
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(_data_source_role_assignment_from_row(row) for row in rows)

    @_locked
    def create_api_credential_revocation(
        self,
        revocation: APICredentialRevocation,
    ) -> APICredentialRevocation:
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO api_credential_revocations
                        (id, tenant_id, credential_id, actor_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revocation.id,
                        revocation.tenant_id,
                        revocation.credential_id,
                        revocation.actor_id,
                        revocation.reason,
                        revocation.created_at.isoformat(),
                    ),
                )
                self._append_audit(
                    AuditEvent(
                        tenant_id=revocation.tenant_id,
                        event_type="security.api_credential_revoked",
                        subject_type="api_credential",
                        subject_id=revocation.credential_id,
                        details={
                            "revocation_id": revocation.id,
                            "actor_id": revocation.actor_id,
                        },
                    )
                )
        except sqlite3.IntegrityError as error:
            raise SecurityConflictError(
                "API credential is already revoked or does not exist"
            ) from error
        return revocation

    @_locked
    def get_api_credential_revocation(
        self,
        tenant_id: str,
        credential_id: str,
    ) -> APICredentialRevocation | None:
        row = self._connection.execute(
            """
            SELECT * FROM api_credential_revocations
            WHERE tenant_id = ? AND credential_id = ?
            """,
            (tenant_id, credential_id),
        ).fetchone()
        return _api_credential_revocation_from_row(row) if row is not None else None

    @_locked
    def list_api_credential_revocations(
        self,
        tenant_id: str,
    ) -> tuple[APICredentialRevocation, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM api_credential_revocations
            WHERE tenant_id = ?
            ORDER BY created_at, id
            """,
            (tenant_id,),
        ).fetchall()
        return tuple(_api_credential_revocation_from_row(row) for row in rows)

    @_locked
    def create_data_source(
        self,
        *,
        tenant_id: str,
        name: str,
        source_type: DataSourceType,
        dialect: str,
        capabilities: Iterable[DataSourceCapability] = (),
        connection_secret_ref: str | None = None,
    ) -> DataSource:
        self._require_tenant(tenant_id)
        data_source = DataSource(
            tenant_id=tenant_id,
            name=name.strip(),
            source_type=source_type,
            dialect=dialect.strip().lower(),
            capabilities=frozenset(capabilities),
            connection_secret_ref=(
                connection_secret_ref.strip() if connection_secret_ref is not None else None
            ),
        )
        capabilities_json = json.dumps(
            sorted(capability.value for capability in data_source.capabilities)
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO data_sources
                    (id, tenant_id, name, source_type, dialect, capabilities_json,
                     connection_secret_ref, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data_source.id,
                    data_source.tenant_id,
                    data_source.name,
                    data_source.source_type.value,
                    data_source.dialect,
                    capabilities_json,
                    data_source.connection_secret_ref,
                    data_source.created_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="data_source.created",
                    subject_type="data_source",
                    subject_id=data_source.id,
                    details={
                        "source_type": data_source.source_type.value,
                        "dialect": data_source.dialect,
                    },
                )
            )
        return data_source

    @_locked
    def get_data_source(self, tenant_id: str, data_source_id: str) -> DataSource | None:
        row = self._connection.execute(
            """
            SELECT * FROM data_sources
            WHERE tenant_id = ? AND id = ?
            """,
            (tenant_id, data_source_id),
        ).fetchone()
        if row is None:
            return None
        return _data_source_from_row(row)

    @_locked
    def list_data_sources(self, tenant_id: str) -> tuple[DataSource, ...]:
        self._require_tenant(tenant_id)
        rows = self._connection.execute(
            """
            SELECT * FROM data_sources
            WHERE tenant_id = ?
            ORDER BY name COLLATE NOCASE, id
            """,
            (tenant_id,),
        ).fetchall()
        return tuple(_data_source_from_row(row) for row in rows)

    @_locked
    def create_query_request(self, query_request: QueryRequest) -> QueryRequest:
        data_source = self.get_data_source(
            query_request.tenant_id,
            query_request.data_source_id,
        )
        if data_source is None:
            raise LookupError("DataSource does not exist in this tenant")
        self._require_catalog_version(
            query_request.tenant_id,
            query_request.catalog_version_id,
            query_request.data_source_id,
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO query_requests
                    (id, tenant_id, data_source_id, catalog_version_id, sql_text,
                     normalized_sql, referenced_tables_json, referenced_columns_json,
                     validation_issue_codes_json, state, business_concepts_json,
                     metrics_json, business_rules_json, assumptions_json,
                     provider_id, model_id, llm_usage_event_id,
                     estimated_db_cost, estimated_db_rows, explained_at,
                     parameter_definitions_json, parameter_names_json,
                     parameter_value_hash, output_lineage_json,
                     output_lineage_complete, approved_by, approved_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_request.id,
                    query_request.tenant_id,
                    query_request.data_source_id,
                    query_request.catalog_version_id,
                    query_request.sql_text,
                    query_request.normalized_sql,
                    json.dumps(query_request.referenced_tables),
                    json.dumps(query_request.referenced_columns),
                    json.dumps(query_request.validation_issue_codes),
                    query_request.state.value,
                    json.dumps(query_request.business_concepts),
                    json.dumps(query_request.metrics),
                    json.dumps(query_request.business_rules),
                    json.dumps(query_request.assumptions),
                    query_request.provider_id,
                    query_request.model_id,
                    query_request.llm_usage_event_id,
                    query_request.estimated_db_cost,
                    query_request.estimated_db_rows,
                    (
                        query_request.explained_at.isoformat()
                        if query_request.explained_at is not None
                        else None
                    ),
                    json.dumps(
                        [
                            {
                                "name": definition.name,
                                "value_type": definition.value_type.value,
                                "nullable": definition.nullable,
                            }
                            for definition in query_request.parameter_definitions
                        ]
                    ),
                    json.dumps(query_request.parameter_names),
                    query_request.parameter_value_hash,
                    json.dumps(
                        [
                            {
                                "output_name": item.output_name,
                                "source_columns": item.source_columns,
                            }
                            for item in query_request.output_lineage
                        ]
                    ),
                    int(query_request.output_lineage_complete),
                    query_request.approved_by,
                    (
                        query_request.approved_at.isoformat()
                        if query_request.approved_at is not None
                        else None
                    ),
                    query_request.created_at.isoformat(),
                    query_request.updated_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=query_request.tenant_id,
                    event_type="query.request_created",
                    subject_type="query_request",
                    subject_id=query_request.id,
                    details={
                        "data_source_id": query_request.data_source_id,
                        "catalog_version_id": query_request.catalog_version_id,
                        "state": query_request.state.value,
                        "table_count": len(query_request.referenced_tables),
                        "column_count": len(query_request.referenced_columns),
                        "validation_issue_codes": query_request.validation_issue_codes,
                    },
                )
            )
        return query_request

    @_locked
    def get_query_request(
        self,
        tenant_id: str,
        request_id: str,
    ) -> QueryRequest | None:
        row = self._connection.execute(
            "SELECT * FROM query_requests WHERE tenant_id = ? AND id = ?",
            (tenant_id, request_id),
        ).fetchone()
        return _query_request_from_row(row) if row is not None else None

    @_locked
    def transition_query_request(
        self,
        tenant_id: str,
        request_id: str,
        target: QueryRequestState,
        *,
        actor_id: str | None = None,
    ) -> QueryRequest:
        current = self.get_query_request(tenant_id, request_id)
        if current is None:
            raise LookupError("Query request not found")
        QueryLifecycle(request_id=current.id, state=current.state).transition(target)
        approved_by: str | None
        approved_at: datetime | None
        if target is QueryRequestState.APPROVED:
            if actor_id is None or not actor_id.strip():
                raise ValueError("Approval requires a non-blank actor")
            approved_by = actor_id.strip()
            approved_at = utc_now()
        elif actor_id is not None:
            raise ValueError("An actor can only be supplied for approval")
        else:
            approved_by = current.approved_by
            approved_at = current.approved_at
        updated_at = utc_now()
        transition_details: dict[str, object] = {
            "from_state": current.state.value,
            "to_state": target.value,
        }
        if target is QueryRequestState.APPROVED:
            transition_details["actor_id"] = approved_by
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE query_requests
                SET state = ?, approved_by = ?, approved_at = ?, updated_at = ?
                WHERE tenant_id = ? AND id = ? AND state = ?
                """,
                (
                    target.value,
                    approved_by,
                    approved_at.isoformat() if approved_at is not None else None,
                    updated_at.isoformat(),
                    tenant_id,
                    request_id,
                    current.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Query request state changed concurrently")
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="query.state_transitioned",
                    subject_type="query_request",
                    subject_id=request_id,
                    details=transition_details,
                )
            )
        return replace(
            current,
            state=target,
            approved_by=approved_by,
            approved_at=approved_at,
            updated_at=updated_at,
        )

    @_locked
    def record_query_activity(
        self,
        tenant_id: str,
        request_id: str,
        event_type: str,
        details: Mapping[str, object],
    ) -> None:
        if event_type not in {"query.explained", "query.result_metadata"}:
            raise ValueError("Unsupported query activity event")
        if self.get_query_request(tenant_id, request_id) is None:
            raise LookupError("Query request not found")
        with self._connection:
            if event_type == "query.explained":
                estimated_cost = details.get("estimated_total_cost")
                estimated_rows = details.get("estimated_rows")
                parameter_names = details.get("parameter_names", ())
                parameter_value_hash = details.get("parameter_value_hash")
                explained_at = utc_now()
                self._connection.execute(
                    """
                    UPDATE query_requests
                    SET estimated_db_cost = ?, estimated_db_rows = ?, explained_at = ?,
                        parameter_names_json = ?, parameter_value_hash = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    (
                        estimated_cost,
                        estimated_rows,
                        explained_at.isoformat(),
                        json.dumps(parameter_names),
                        parameter_value_hash,
                        tenant_id,
                        request_id,
                    ),
                )
                audit_details = {
                    key: value
                    for key, value in details.items()
                    if key != "parameter_value_hash"
                }
            else:
                audit_details = dict(details)
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type=event_type,
                    subject_type="query_request",
                    subject_id=request_id,
                    details=audit_details,
                )
            )

    @_locked
    def record_data_source_activity(
        self,
        tenant_id: str,
        data_source_id: str,
        event_type: str,
        details: Mapping[str, object],
    ) -> None:
        if event_type not in {
            "data_source.connection_test_succeeded",
            "data_source.connection_test_failed",
        }:
            raise ValueError("Unsupported DataSource activity event")
        if self.get_data_source(tenant_id, data_source_id) is None:
            raise LookupError("DataSource does not exist in this tenant")
        with self._connection:
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type=event_type,
                    subject_type="data_source",
                    subject_id=data_source_id,
                    details=dict(details),
                )
            )

    @_locked
    def create_corrected_sql_example(
        self,
        example: CorrectedSQLExample,
    ) -> CorrectedSQLExample:
        if self.get_data_source(example.tenant_id, example.data_source_id) is None:
            raise LookupError("DataSource does not exist in this tenant")
        self._require_catalog_version(
            example.tenant_id,
            example.catalog_version_id,
            example.data_source_id,
        )
        if example.source_query_request_id is not None:
            source_row = self._connection.execute(
                """
                SELECT 1 FROM query_requests
                WHERE tenant_id = ? AND data_source_id = ? AND id = ?
                """,
                (
                    example.tenant_id,
                    example.data_source_id,
                    example.source_query_request_id,
                ),
            ).fetchone()
            if source_row is None:
                raise LookupError("Source query request does not exist in this DataSource")
        if example.supersedes_example_id is not None:
            predecessor = self._connection.execute(
                """
                SELECT predecessor.normalized_question, predecessor.revision
                FROM corrected_sql_examples AS predecessor
                WHERE predecessor.tenant_id = ?
                  AND predecessor.data_source_id = ?
                  AND predecessor.id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM corrected_sql_examples AS successor
                      WHERE successor.tenant_id = predecessor.tenant_id
                        AND successor.supersedes_example_id = predecessor.id
                  )
                """,
                (
                    example.tenant_id,
                    example.data_source_id,
                    example.supersedes_example_id,
                ),
            ).fetchone()
            if (
                predecessor is None
                or predecessor["normalized_question"] != example.normalized_question
                or int(predecessor["revision"]) + 1 != example.revision
            ):
                raise CorrectedSQLExampleConflictError(
                    "Corrected SQL predecessor is stale or does not match the question"
                )
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO corrected_sql_examples
                        (id, tenant_id, data_source_id, catalog_version_id, question,
                         normalized_question, content_classification, sql_text,
                         normalized_sql, referenced_tables_json, referenced_columns_json,
                         business_concepts_json, assumptions_json, actor_id, reason,
                         source_query_request_id, supersedes_example_id, revision, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        example.id,
                        example.tenant_id,
                        example.data_source_id,
                        example.catalog_version_id,
                        example.question,
                        example.normalized_question,
                        example.content_classification.value,
                        example.sql_text,
                        example.normalized_sql,
                        json.dumps(example.referenced_tables),
                        json.dumps(example.referenced_columns),
                        json.dumps(example.business_concepts),
                        json.dumps(example.assumptions),
                        example.actor_id,
                        example.reason,
                        example.source_query_request_id,
                        example.supersedes_example_id,
                        example.revision,
                        example.created_at.isoformat(),
                    ),
                )
                self._append_audit(
                    AuditEvent(
                        tenant_id=example.tenant_id,
                        event_type="learning.corrected_sql_recorded",
                        subject_type="corrected_sql_example",
                        subject_id=example.id,
                        details={
                            "data_source_id": example.data_source_id,
                            "catalog_version_id": example.catalog_version_id,
                            "revision": example.revision,
                            "table_count": len(example.referenced_tables),
                            "column_count": len(example.referenced_columns),
                            "source_query_request_id": example.source_query_request_id,
                            "supersedes_example_id": example.supersedes_example_id,
                        },
                    )
                )
        except sqlite3.IntegrityError as error:
            raise CorrectedSQLExampleConflictError(
                "Corrected SQL example conflicts with the current revision"
            ) from error
        return example

    @_locked
    def get_corrected_sql_example(
        self,
        tenant_id: str,
        data_source_id: str,
        example_id: str,
    ) -> CorrectedSQLExample | None:
        row = self._connection.execute(
            """
            SELECT * FROM corrected_sql_examples
            WHERE tenant_id = ? AND data_source_id = ? AND id = ?
            """,
            (tenant_id, data_source_id, example_id),
        ).fetchone()
        return _corrected_sql_example_from_row(row) if row is not None else None

    @_locked
    def list_corrected_sql_examples(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[CorrectedSQLExample, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM corrected_sql_examples
            WHERE tenant_id = ? AND data_source_id = ?
            ORDER BY normalized_question, revision, created_at, id
            """,
            (tenant_id, data_source_id),
        ).fetchall()
        return tuple(_corrected_sql_example_from_row(row) for row in rows)

    @_locked
    def create_query_feedback_event(
        self,
        event: QueryFeedbackEvent,
    ) -> QueryFeedbackEvent:
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO query_feedback_events
                        (id, tenant_id, data_source_id, query_request_id, outcome,
                         actor_id, reason, corrected_sql_example_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.tenant_id,
                        event.data_source_id,
                        event.query_request_id,
                        event.outcome.value,
                        event.actor_id,
                        event.reason,
                        event.corrected_sql_example_id,
                        event.created_at.isoformat(),
                    ),
                )
                self._append_audit(
                    AuditEvent(
                        tenant_id=event.tenant_id,
                        event_type="learning.query_feedback_recorded",
                        subject_type="query_request",
                        subject_id=event.query_request_id,
                        details={
                            "feedback_id": event.id,
                            "data_source_id": event.data_source_id,
                            "outcome": event.outcome.value,
                            "actor_id": event.actor_id,
                            "corrected_sql_example_id": event.corrected_sql_example_id,
                        },
                    )
                )
        except sqlite3.IntegrityError as error:
            raise LearningGovernanceConflictError(
                "Query request already has final feedback or a feedback link is invalid"
            ) from error
        return event

    @_locked
    def get_query_feedback_event(
        self,
        tenant_id: str,
        query_request_id: str,
    ) -> QueryFeedbackEvent | None:
        row = self._connection.execute(
            """
            SELECT * FROM query_feedback_events
            WHERE tenant_id = ? AND query_request_id = ?
            """,
            (tenant_id, query_request_id),
        ).fetchone()
        return _query_feedback_event_from_row(row) if row is not None else None

    @_locked
    def list_query_feedback_events(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[QueryFeedbackEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM query_feedback_events
            WHERE tenant_id = ? AND data_source_id = ?
            ORDER BY created_at, id
            """,
            (tenant_id, data_source_id),
        ).fetchall()
        return tuple(_query_feedback_event_from_row(row) for row in rows)

    @_locked
    def create_golden_evaluation_candidate(
        self,
        candidate: GoldenEvaluationCandidate,
    ) -> GoldenEvaluationCandidate:
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO golden_evaluation_candidates
                        (id, tenant_id, data_source_id, catalog_version_id,
                         corrected_sql_example_id, source_query_request_id, question,
                         normalized_sql, referenced_tables_json,
                         referenced_columns_json, business_concepts_json,
                         assumptions_json, content_classification, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.id,
                        candidate.tenant_id,
                        candidate.data_source_id,
                        candidate.catalog_version_id,
                        candidate.corrected_sql_example_id,
                        candidate.source_query_request_id,
                        candidate.question,
                        candidate.normalized_sql,
                        json.dumps(candidate.referenced_tables),
                        json.dumps(candidate.referenced_columns),
                        json.dumps(candidate.business_concepts),
                        json.dumps(candidate.assumptions),
                        candidate.content_classification.value,
                        candidate.created_at.isoformat(),
                    ),
                )
                self._append_audit(
                    AuditEvent(
                        tenant_id=candidate.tenant_id,
                        event_type="evaluation.golden_candidate_proposed",
                        subject_type="golden_evaluation_candidate",
                        subject_id=candidate.id,
                        details={
                            "data_source_id": candidate.data_source_id,
                            "catalog_version_id": candidate.catalog_version_id,
                            "corrected_sql_example_id": candidate.corrected_sql_example_id,
                            "source_query_request_id": candidate.source_query_request_id,
                            "table_count": len(candidate.referenced_tables),
                            "column_count": len(candidate.referenced_columns),
                        },
                    )
                )
        except sqlite3.IntegrityError as error:
            raise LearningGovernanceConflictError(
                "Corrected SQL example is already a golden candidate or has invalid links"
            ) from error
        return candidate

    @_locked
    def get_golden_evaluation_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> GoldenEvaluationCandidate | None:
        row = self._connection.execute(
            """
            SELECT * FROM golden_evaluation_candidates
            WHERE tenant_id = ? AND id = ?
            """,
            (tenant_id, candidate_id),
        ).fetchone()
        return _golden_evaluation_candidate_from_row(row) if row is not None else None

    @_locked
    def list_golden_evaluation_candidates(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[GoldenEvaluationCandidate, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM golden_evaluation_candidates
            WHERE tenant_id = ? AND data_source_id = ?
            ORDER BY created_at, id
            """,
            (tenant_id, data_source_id),
        ).fetchall()
        return tuple(_golden_evaluation_candidate_from_row(row) for row in rows)

    @_locked
    def create_golden_candidate_review(
        self,
        review: GoldenCandidateReview,
    ) -> GoldenCandidateReview:
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO golden_candidate_reviews
                        (id, tenant_id, candidate_id, decision, actor_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review.id,
                        review.tenant_id,
                        review.candidate_id,
                        review.status.value,
                        review.actor_id,
                        review.reason,
                        review.created_at.isoformat(),
                    ),
                )
                self._append_audit(
                    AuditEvent(
                        tenant_id=review.tenant_id,
                        event_type="evaluation.golden_candidate_reviewed",
                        subject_type="golden_evaluation_candidate",
                        subject_id=review.candidate_id,
                        details={
                            "review_id": review.id,
                            "decision": review.status.value,
                            "actor_id": review.actor_id,
                        },
                    )
                )
        except sqlite3.IntegrityError as error:
            raise LearningGovernanceConflictError(
                "Golden candidate already has a final review or does not exist"
            ) from error
        return review

    @_locked
    def get_golden_candidate_review(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> GoldenCandidateReview | None:
        row = self._connection.execute(
            """
            SELECT * FROM golden_candidate_reviews
            WHERE tenant_id = ? AND candidate_id = ?
            """,
            (tenant_id, candidate_id),
        ).fetchone()
        return _golden_candidate_review_from_row(row) if row is not None else None

    @_locked
    def list_golden_candidate_reviews(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[GoldenCandidateReview, ...]:
        rows = self._connection.execute(
            """
            SELECT review.*
            FROM golden_candidate_reviews AS review
            JOIN golden_evaluation_candidates AS candidate
              ON candidate.tenant_id = review.tenant_id
             AND candidate.id = review.candidate_id
            WHERE review.tenant_id = ? AND candidate.data_source_id = ?
            ORDER BY review.created_at, review.id
            """,
            (tenant_id, data_source_id),
        ).fetchall()
        return tuple(_golden_candidate_review_from_row(row) for row in rows)

    @_locked
    def create_catalog_version(self, tenant_id: str, data_source_id: str) -> CatalogVersion:
        if self.get_data_source(tenant_id, data_source_id) is None:
            raise LookupError("DataSource does not exist in this tenant")
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM catalog_versions
            WHERE tenant_id = ? AND data_source_id = ?
            """,
            (tenant_id, data_source_id),
        ).fetchone()
        version = CatalogVersion(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            version=int(row["next_version"]),
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO catalog_versions (id, tenant_id, data_source_id, version, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    version.id,
                    version.tenant_id,
                    version.data_source_id,
                    version.version,
                    version.created_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="catalog.version_created",
                    subject_type="catalog_version",
                    subject_id=version.id,
                    details={"version": version.version, "data_source_id": data_source_id},
                )
            )
        return version

    @_locked
    def get_latest_catalog_version(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> CatalogVersion | None:
        row = self._connection.execute(
            """
            SELECT * FROM catalog_versions
            WHERE tenant_id = ? AND data_source_id = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (tenant_id, data_source_id),
        ).fetchone()
        if row is None:
            return None
        return CatalogVersion(
            id=row["id"],
            tenant_id=row["tenant_id"],
            data_source_id=row["data_source_id"],
            version=int(row["version"]),
            created_at=_parse_datetime(row["created_at"]),
        )

    @_locked
    def create_schema_object(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        catalog_version_id: str,
        schema_name: str,
        name: str,
        kind: ObjectKind,
        definition_sql: str | None = None,
    ) -> SchemaObject:
        self._require_catalog_version(tenant_id, catalog_version_id, data_source_id)
        schema_object = SchemaObject(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog_version_id=catalog_version_id,
            schema_name=schema_name.strip(),
            name=name.strip(),
            kind=kind,
            definition_sql=definition_sql,
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO schema_objects
                    (id, tenant_id, data_source_id, catalog_version_id,
                     schema_name, object_name, object_kind, definition_sql)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schema_object.id,
                    schema_object.tenant_id,
                    schema_object.data_source_id,
                    schema_object.catalog_version_id,
                    schema_object.schema_name,
                    schema_object.name,
                    schema_object.kind.value,
                    schema_object.definition_sql,
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="catalog.schema_object_created",
                    subject_type="schema_object",
                    subject_id=schema_object.id,
                    details={
                        "catalog_version_id": catalog_version_id,
                        "object_kind": kind.value,
                    },
                )
            )
        return schema_object

    @_locked
    def list_schema_objects(
        self,
        tenant_id: str,
        catalog_version_id: str,
    ) -> tuple[SchemaObject, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM schema_objects
            WHERE tenant_id = ? AND catalog_version_id = ?
            ORDER BY schema_name, object_name
            """,
            (tenant_id, catalog_version_id),
        ).fetchall()
        return tuple(
            SchemaObject(
                id=row["id"],
                tenant_id=row["tenant_id"],
                data_source_id=row["data_source_id"],
                catalog_version_id=row["catalog_version_id"],
                schema_name=row["schema_name"],
                name=row["object_name"],
                kind=ObjectKind(row["object_kind"]),
                definition_sql=row["definition_sql"],
            )
            for row in rows
        )

    @_locked
    def create_column(
        self,
        *,
        tenant_id: str,
        schema_object_id: str,
        name: str,
        physical_type: str,
        ordinal: int,
        nullable: bool = True,
        classification: Classification = Classification.INTERNAL,
        default_expression: str | None = None,
        is_primary_key: bool = False,
    ) -> ColumnDefinition:
        self._require_schema_object(tenant_id, schema_object_id)
        column = ColumnDefinition(
            tenant_id=tenant_id,
            schema_object_id=schema_object_id,
            name=name.strip(),
            physical_type=physical_type.strip(),
            ordinal=ordinal,
            nullable=nullable,
            classification=classification,
            default_expression=default_expression,
            is_primary_key=is_primary_key,
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO column_definitions
                    (id, tenant_id, schema_object_id, column_name, physical_type,
                     ordinal, nullable, classification, default_expression, is_primary_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    column.id,
                    column.tenant_id,
                    column.schema_object_id,
                    column.name,
                    column.physical_type,
                    column.ordinal,
                    column.nullable,
                    column.classification.value,
                    column.default_expression,
                    column.is_primary_key,
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="catalog.column_created",
                    subject_type="column",
                    subject_id=column.id,
                    details={
                        "schema_object_id": schema_object_id,
                        "classification": classification.value,
                    },
                )
            )
        return column

    @_locked
    def list_columns(self, tenant_id: str, schema_object_id: str) -> tuple[ColumnDefinition, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM column_definitions
            WHERE tenant_id = ? AND schema_object_id = ?
            ORDER BY ordinal
            """,
            (tenant_id, schema_object_id),
        ).fetchall()
        return tuple(_column_definition_from_row(row) for row in rows)

    @_locked
    def list_columns_for_catalog_version(
        self,
        tenant_id: str,
        catalog_version_id: str,
    ) -> tuple[ColumnDefinition, ...]:
        rows = self._connection.execute(
            """
            SELECT column_definition.*
            FROM column_definitions AS column_definition
            JOIN schema_objects AS schema_object
              ON schema_object.tenant_id = column_definition.tenant_id
             AND schema_object.id = column_definition.schema_object_id
            WHERE column_definition.tenant_id = ?
              AND schema_object.catalog_version_id = ?
            ORDER BY schema_object.schema_name, schema_object.object_name,
                     column_definition.ordinal
            """,
            (tenant_id, catalog_version_id),
        ).fetchall()
        return tuple(_column_definition_from_row(row) for row in rows)

    @_locked
    def create_relationship(
        self,
        *,
        tenant_id: str,
        catalog_version_id: str,
        source_object_id: str,
        target_object_id: str,
        name: str,
        source_columns: tuple[str, ...],
        target_columns: tuple[str, ...],
        status: EpistemicStatus = EpistemicStatus.IMPORTED,
        source: str = "database_foreign_key",
        confidence: float = 1.0,
    ) -> Relationship:
        self._require_catalog_version(tenant_id, catalog_version_id)
        self._require_schema_object(tenant_id, source_object_id)
        self._require_schema_object(tenant_id, target_object_id)
        relationship = Relationship(
            tenant_id=tenant_id,
            catalog_version_id=catalog_version_id,
            source_object_id=source_object_id,
            target_object_id=target_object_id,
            name=name.strip(),
            source_columns=source_columns,
            target_columns=target_columns,
            status=status,
            source=source,
            confidence=confidence,
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO relationships
                    (id, tenant_id, catalog_version_id, source_object_id, target_object_id,
                     relationship_name, source_columns_json, target_columns_json,
                     epistemic_status, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relationship.id,
                    relationship.tenant_id,
                    relationship.catalog_version_id,
                    relationship.source_object_id,
                    relationship.target_object_id,
                    relationship.name,
                    json.dumps(relationship.source_columns),
                    json.dumps(relationship.target_columns),
                    relationship.status.value,
                    relationship.source,
                    relationship.confidence,
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="catalog.relationship_created",
                    subject_type="relationship",
                    subject_id=relationship.id,
                    details={
                        "catalog_version_id": catalog_version_id,
                        "source_object_id": source_object_id,
                        "target_object_id": target_object_id,
                    },
                )
            )
        return relationship

    @_locked
    def list_relationships(
        self,
        tenant_id: str,
        catalog_version_id: str,
    ) -> tuple[Relationship, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM relationships
            WHERE tenant_id = ? AND catalog_version_id = ?
            ORDER BY relationship_name
            """,
            (tenant_id, catalog_version_id),
        ).fetchall()
        return tuple(
            Relationship(
                id=row["id"],
                tenant_id=row["tenant_id"],
                catalog_version_id=row["catalog_version_id"],
                source_object_id=row["source_object_id"],
                target_object_id=row["target_object_id"],
                name=row["relationship_name"],
                source_columns=tuple(json.loads(row["source_columns_json"])),
                target_columns=tuple(json.loads(row["target_columns_json"])),
                status=EpistemicStatus(row["epistemic_status"]),
                source=row["source"],
                confidence=float(row["confidence"]),
            )
            for row in rows
        )

    @_locked
    def propose_semantic_definition(
        self,
        definition: SemanticDefinition,
        *,
        explicit_supersede: bool = False,
        expected_updated_at: datetime | None = None,
    ) -> SemanticWriteResult:
        self._require_catalog_version(definition.tenant_id, definition.catalog_version_id)
        data_source_id = self._catalog_version_data_source(
            definition.tenant_id,
            definition.catalog_version_id,
        )
        current = self.get_semantic_resolution(
            definition.tenant_id,
            data_source_id,
            definition.object_ref,
        )
        if explicit_supersede:
            _require_expected_resolution(current, expected_updated_at)
            if definition.status is not EpistemicStatus.CONFIRMED:
                raise ValueError("Only CONFIRMED evidence can explicitly supersede a resolution")
            decision = ResolutionDecision(
                ResolutionAction.ACCEPT,
                "Explicit human correction supersedes the current resolution",
            )
        else:
            decision = resolve_semantic_update(current, definition)

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO semantic_definitions
                    (id, tenant_id, catalog_version_id, object_ref, description,
                     epistemic_status, source, confidence, actor_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition.id,
                    definition.tenant_id,
                    definition.catalog_version_id,
                    definition.object_ref,
                    definition.description,
                    definition.status.value,
                    definition.source,
                    definition.confidence,
                    definition.actor_id,
                    definition.reason,
                    definition.created_at.isoformat(),
                ),
            )

            if decision.action is ResolutionAction.ACCEPT:
                resolution = SemanticResolution(
                    tenant_id=definition.tenant_id,
                    data_source_id=data_source_id,
                    object_ref=definition.object_ref,
                    description=definition.description,
                    status=definition.status,
                    confidence=definition.confidence,
                    selected_definition_id=definition.id,
                    updated_at=_next_resolution_timestamp(current),
                )
                self._upsert_resolution(resolution)
            elif decision.action is ResolutionAction.MARK_CONFLICT:
                resolved_current = _require_current_resolution(current, decision)
                resolution = replace(
                    resolved_current,
                    status=EpistemicStatus.CONFLICTING,
                    confidence=min(resolved_current.confidence, definition.confidence),
                    selected_definition_id=None,
                    updated_at=_next_resolution_timestamp(resolved_current),
                )
                self._upsert_resolution(resolution)
            else:
                resolution = _require_current_resolution(current, decision)

            self._append_audit(
                AuditEvent(
                    tenant_id=definition.tenant_id,
                    event_type="semantic.definition_proposed",
                    subject_type="semantic_object",
                    subject_id=definition.object_ref,
                    details={
                        "definition_id": definition.id,
                        "source_status": definition.status.value,
                        "resolution_action": decision.action.value,
                        "resolution_status": resolution.status.value,
                        "actor_id": definition.actor_id,
                        "explicit_supersede": explicit_supersede,
                    },
                )
            )

        return SemanticWriteResult(
            evidence=definition,
            resolution=resolution,
            action=decision.action,
        )

    @_locked
    def list_semantic_definitions(
        self,
        tenant_id: str,
        data_source_id: str,
        object_ref: str,
    ) -> tuple[SemanticDefinition, ...]:
        rows = self._connection.execute(
            """
            SELECT definition.*
            FROM semantic_definitions AS definition
            JOIN catalog_versions AS version
              ON version.tenant_id = definition.tenant_id
             AND version.id = definition.catalog_version_id
            WHERE definition.tenant_id = ?
              AND version.data_source_id = ?
              AND definition.object_ref = ?
            ORDER BY definition.created_at DESC, definition.id DESC
            """,
            (tenant_id, data_source_id, object_ref),
        ).fetchall()
        return tuple(_semantic_definition_from_row(row) for row in rows)

    @_locked
    def list_semantic_resolutions(
        self,
        tenant_id: str,
        data_source_id: str,
        statuses: frozenset[EpistemicStatus] = frozenset(),
    ) -> tuple[SemanticResolution, ...]:
        parameters: list[object] = [tenant_id, data_source_id]
        status_filter = ""
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            status_filter = f" AND epistemic_status IN ({placeholders})"
            parameters.extend(
                status.value for status in sorted(statuses, key=lambda item: item.value)
            )
        rows = self._connection.execute(
            f"""
            SELECT * FROM semantic_resolutions
            WHERE tenant_id = ? AND data_source_id = ?{status_filter}
            ORDER BY updated_at DESC, object_ref
            """,
            parameters,
        ).fetchall()
        return tuple(_semantic_resolution_from_row(row) for row in rows)

    @_locked
    def get_semantic_resolution(
        self,
        tenant_id: str,
        data_source_id: str,
        object_ref: str,
    ) -> SemanticResolution | None:
        row = self._connection.execute(
            """
            SELECT * FROM semantic_resolutions
            WHERE tenant_id = ? AND data_source_id = ? AND object_ref = ?
            """,
            (tenant_id, data_source_id, object_ref),
        ).fetchone()
        if row is None:
            return None
        return _semantic_resolution_from_row(row)

    @_locked
    def propose_business_concept_definition(
        self,
        definition: BusinessConceptDefinition,
        *,
        explicit_supersede: bool = False,
        expected_updated_at: datetime | None = None,
    ) -> BusinessConceptWriteResult:
        self._require_catalog_version(
            definition.tenant_id,
            definition.catalog_version_id,
            definition.data_source_id,
        )
        current = self.get_business_concept_resolution(
            definition.tenant_id,
            definition.data_source_id,
            definition.concept_key,
        )
        if explicit_supersede:
            _require_expected_concept_resolution(current, expected_updated_at)
            if definition.status is not EpistemicStatus.CONFIRMED:
                raise ValueError("Only CONFIRMED evidence can explicitly supersede a concept")
            decision = ResolutionDecision(
                ResolutionAction.ACCEPT,
                "Explicit human correction supersedes the concept resolution",
            )
        else:
            decision = resolve_business_concept_update(current, definition)

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO business_concept_definitions
                    (id, tenant_id, data_source_id, catalog_version_id, concept_key,
                     concept_name, description, synonyms_json, object_refs_json,
                     content_classification, epistemic_status, source, confidence,
                     actor_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition.id,
                    definition.tenant_id,
                    definition.data_source_id,
                    definition.catalog_version_id,
                    definition.concept_key,
                    definition.name,
                    definition.description,
                    json.dumps(definition.synonyms),
                    json.dumps(definition.object_refs),
                    definition.content_classification.value,
                    definition.status.value,
                    definition.source,
                    definition.confidence,
                    definition.actor_id,
                    definition.reason,
                    definition.created_at.isoformat(),
                ),
            )
            if decision.action is ResolutionAction.ACCEPT:
                resolution = BusinessConceptResolution(
                    tenant_id=definition.tenant_id,
                    data_source_id=definition.data_source_id,
                    concept_key=definition.concept_key,
                    name=definition.name,
                    description=definition.description,
                    synonyms=definition.synonyms,
                    object_refs=definition.object_refs,
                    content_classification=definition.content_classification,
                    status=definition.status,
                    confidence=definition.confidence,
                    selected_definition_id=definition.id,
                    updated_at=_next_resolution_timestamp(current),
                )
                self._upsert_business_concept_resolution(resolution)
            elif decision.action is ResolutionAction.MARK_CONFLICT:
                resolved_current = _require_current_resolution(current, decision)
                resolution = replace(
                    resolved_current,
                    status=EpistemicStatus.CONFLICTING,
                    confidence=min(resolved_current.confidence, definition.confidence),
                    selected_definition_id=None,
                    updated_at=_next_resolution_timestamp(resolved_current),
                )
                self._upsert_business_concept_resolution(resolution)
            else:
                resolution = _require_current_resolution(current, decision)
            self._append_audit(
                AuditEvent(
                    tenant_id=definition.tenant_id,
                    event_type="business_concept.definition_proposed",
                    subject_type="business_concept",
                    subject_id=definition.concept_key,
                    details={
                        "definition_id": definition.id,
                        "source_status": definition.status.value,
                        "resolution_action": decision.action.value,
                        "resolution_status": resolution.status.value,
                        "actor_id": definition.actor_id,
                        "explicit_supersede": explicit_supersede,
                        "synonym_count": len(definition.synonyms),
                        "object_ref_count": len(definition.object_refs),
                    },
                )
            )
        return BusinessConceptWriteResult(definition, resolution, decision.action)

    @_locked
    def list_business_concept_definitions(
        self,
        tenant_id: str,
        data_source_id: str,
        concept_key: str,
    ) -> tuple[BusinessConceptDefinition, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM business_concept_definitions
            WHERE tenant_id = ? AND data_source_id = ? AND concept_key = ?
            ORDER BY created_at DESC, id DESC
            """,
            (tenant_id, data_source_id, concept_key),
        ).fetchall()
        return tuple(_business_concept_definition_from_row(row) for row in rows)

    @_locked
    def list_business_concept_resolutions(
        self,
        tenant_id: str,
        data_source_id: str,
        statuses: frozenset[EpistemicStatus] = frozenset(),
    ) -> tuple[BusinessConceptResolution, ...]:
        parameters: list[object] = [tenant_id, data_source_id]
        status_filter = ""
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            status_filter = f" AND epistemic_status IN ({placeholders})"
            parameters.extend(
                status.value for status in sorted(statuses, key=lambda item: item.value)
            )
        rows = self._connection.execute(
            f"""
            SELECT * FROM business_concept_resolutions
            WHERE tenant_id = ? AND data_source_id = ?{status_filter}
            ORDER BY updated_at DESC, concept_key
            """,
            parameters,
        ).fetchall()
        return tuple(_business_concept_resolution_from_row(row) for row in rows)

    @_locked
    def get_business_concept_resolution(
        self,
        tenant_id: str,
        data_source_id: str,
        concept_key: str,
    ) -> BusinessConceptResolution | None:
        row = self._connection.execute(
            """
            SELECT * FROM business_concept_resolutions
            WHERE tenant_id = ? AND data_source_id = ? AND concept_key = ?
            """,
            (tenant_id, data_source_id, concept_key),
        ).fetchone()
        return _business_concept_resolution_from_row(row) if row is not None else None

    @_locked
    def propose_analytic_semantic_definition(
        self,
        definition: AnalyticSemanticDefinition,
        *,
        explicit_supersede: bool = False,
        expected_updated_at: datetime | None = None,
    ) -> AnalyticSemanticWriteResult:
        self._require_catalog_version(
            definition.tenant_id,
            definition.catalog_version_id,
            definition.data_source_id,
        )
        kind, asset_key = _analytic_kind_and_key(definition)
        current = self.get_analytic_semantic_resolution(
            definition.tenant_id,
            definition.data_source_id,
            kind,
            asset_key,
        )
        if explicit_supersede:
            _require_expected_analytic_resolution(current, expected_updated_at)
            if definition.status is not EpistemicStatus.CONFIRMED:
                raise ValueError("Only CONFIRMED evidence can explicitly supersede an asset")
            decision = ResolutionDecision(
                ResolutionAction.ACCEPT,
                "Explicit human correction supersedes the analytic semantic resolution",
            )
        elif isinstance(definition, MetricDefinition):
            metric_current = current if isinstance(current, MetricResolution) else None
            decision = resolve_metric_update(metric_current, definition)
        else:
            rule_current = current if isinstance(current, BusinessRuleResolution) else None
            decision = resolve_business_rule_update(rule_current, definition)

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO analytic_semantic_definitions
                    (id, tenant_id, data_source_id, catalog_version_id, asset_kind,
                     asset_key, payload_json, content_classification, epistemic_status,
                     source, confidence, actor_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition.id,
                    definition.tenant_id,
                    definition.data_source_id,
                    definition.catalog_version_id,
                    kind.value,
                    asset_key,
                    json.dumps(_analytic_payload(definition), sort_keys=True),
                    definition.content_classification.value,
                    definition.status.value,
                    definition.source,
                    definition.confidence,
                    definition.actor_id,
                    definition.reason,
                    definition.created_at.isoformat(),
                ),
            )
            if decision.action is ResolutionAction.ACCEPT:
                resolution = _analytic_resolution_from_definition(
                    definition,
                    updated_at=_next_resolution_timestamp(current),
                )
                self._upsert_analytic_semantic_resolution(resolution)
            elif decision.action is ResolutionAction.MARK_CONFLICT:
                resolved_current = cast(
                    AnalyticSemanticResolution,
                    _require_current_resolution(current, decision),
                )
                resolution = replace(
                    resolved_current,
                    status=EpistemicStatus.CONFLICTING,
                    confidence=min(resolved_current.confidence, definition.confidence),
                    selected_definition_id=None,
                    updated_at=_next_resolution_timestamp(resolved_current),
                )
                self._upsert_analytic_semantic_resolution(resolution)
            else:
                resolution = cast(
                    AnalyticSemanticResolution,
                    _require_current_resolution(current, decision),
                )
            self._append_audit(
                AuditEvent(
                    tenant_id=definition.tenant_id,
                    event_type="analytic_semantic.definition_proposed",
                    subject_type=kind.value,
                    subject_id=asset_key,
                    details={
                        "definition_id": definition.id,
                        "asset_kind": kind.value,
                        "source_status": definition.status.value,
                        "resolution_action": decision.action.value,
                        "resolution_status": resolution.status.value,
                        "actor_id": definition.actor_id,
                        "explicit_supersede": explicit_supersede,
                        "object_ref_count": len(definition.object_refs),
                        "concept_key_count": len(definition.concept_keys),
                    },
                )
            )
        return AnalyticSemanticWriteResult(definition, resolution, decision.action)

    @_locked
    def list_analytic_semantic_definitions(
        self,
        tenant_id: str,
        data_source_id: str,
        kind: AnalyticSemanticKind,
        asset_key: str,
    ) -> tuple[AnalyticSemanticDefinition, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM analytic_semantic_definitions
            WHERE tenant_id = ? AND data_source_id = ?
              AND asset_kind = ? AND asset_key = ?
            ORDER BY created_at DESC, id DESC
            """,
            (tenant_id, data_source_id, kind.value, asset_key),
        ).fetchall()
        return tuple(_analytic_definition_from_row(row) for row in rows)

    @_locked
    def list_analytic_semantic_resolutions(
        self,
        tenant_id: str,
        data_source_id: str,
        *,
        kind: AnalyticSemanticKind | None = None,
        statuses: frozenset[EpistemicStatus] = frozenset(),
    ) -> tuple[AnalyticSemanticResolution, ...]:
        parameters: list[object] = [tenant_id, data_source_id]
        filters = ""
        if kind is not None:
            filters += " AND asset_kind = ?"
            parameters.append(kind.value)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            filters += f" AND epistemic_status IN ({placeholders})"
            parameters.extend(
                status.value for status in sorted(statuses, key=lambda item: item.value)
            )
        rows = self._connection.execute(
            f"""
            SELECT * FROM analytic_semantic_resolutions
            WHERE tenant_id = ? AND data_source_id = ?{filters}
            ORDER BY updated_at DESC, asset_kind, asset_key
            """,
            parameters,
        ).fetchall()
        return tuple(_analytic_resolution_from_row(row) for row in rows)

    @_locked
    def get_analytic_semantic_resolution(
        self,
        tenant_id: str,
        data_source_id: str,
        kind: AnalyticSemanticKind,
        asset_key: str,
    ) -> AnalyticSemanticResolution | None:
        row = self._connection.execute(
            """
            SELECT * FROM analytic_semantic_resolutions
            WHERE tenant_id = ? AND data_source_id = ?
              AND asset_kind = ? AND asset_key = ?
            """,
            (tenant_id, data_source_id, kind.value, asset_key),
        ).fetchone()
        return _analytic_resolution_from_row(row) if row is not None else None

    @_locked
    def audit_events(self, tenant_id: str) -> tuple[AuditEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM audit_events WHERE tenant_id = ? ORDER BY created_at, id",
            (tenant_id,),
        ).fetchall()
        return tuple(
            AuditEvent(
                id=row["id"],
                tenant_id=row["tenant_id"],
                event_type=row["event_type"],
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                details=json.loads(row["details_json"]),
                created_at=_parse_datetime(row["created_at"]),
            )
            for row in rows
        )

    @_locked
    def record_llm_usage(self, event: LLMUsageEvent) -> None:
        self._require_tenant(event.tenant_id)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO llm_usage_events
                    (id, tenant_id, provider_id, model_id, purpose,
                     estimated_input_tokens, estimated_output_tokens,
                     input_tokens, cached_input_tokens, output_tokens, latency_ms,
                     estimated_cost, actual_cost, currency, pricing_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.tenant_id,
                    event.provider_id,
                    event.model_id,
                    event.purpose,
                    event.estimated_input_tokens,
                    event.estimated_output_tokens,
                    event.input_tokens,
                    event.cached_input_tokens,
                    event.output_tokens,
                    event.latency_ms,
                    event.estimated_cost,
                    event.actual_cost,
                    event.currency,
                    event.pricing_id,
                    event.created_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=event.tenant_id,
                    event_type="llm.usage_recorded",
                    subject_type="llm_usage_event",
                    subject_id=event.id,
                    details={
                        "provider_id": event.provider_id,
                        "model_id": event.model_id,
                        "purpose": event.purpose,
                        "input_tokens": event.input_tokens,
                        "output_tokens": event.output_tokens,
                        "latency_ms": event.latency_ms,
                    },
                )
            )

    @_locked
    def list_llm_usage_events(self, tenant_id: str) -> tuple[LLMUsageEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM llm_usage_events
            WHERE tenant_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (tenant_id,),
        ).fetchall()
        return tuple(_llm_usage_from_row(row) for row in rows)

    @_locked
    def get_llm_usage_event(
        self,
        tenant_id: str,
        event_id: str,
    ) -> LLMUsageEvent | None:
        row = self._connection.execute(
            """
            SELECT * FROM llm_usage_events
            WHERE tenant_id = ? AND id = ?
            """,
            (tenant_id, event_id),
        ).fetchone()
        return _llm_usage_from_row(row) if row is not None else None

    @_locked
    def record_ai_transfer_receipt(
        self,
        receipt: AITransferReceipt,
    ) -> AITransferReceipt:
        self._require_tenant(receipt.tenant_id)
        if self.get_data_source(receipt.tenant_id, receipt.data_source_id) is None:
            raise LookupError("DataSource does not exist in this tenant")
        content_counts = tuple(
            {
                "kind": item.kind,
                "classification": item.classification.value,
                "included_count": item.included_count,
                "redacted_count": item.redacted_count,
            }
            for item in receipt.content_counts
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO ai_transfer_receipts
                    (id, tenant_id, data_source_id, actor_id, provider_id, model_id,
                     purpose, privacy_mode, provider_policy_id, policy_scope,
                     provider_policy_version, declared_classification,
                     detected_classification, effective_classification,
                     maximum_allowed_classification, detection_reason_codes_json,
                     content_counts_json, preflight_digest, confirmation_outcome,
                     provider_invoked, decision_code, llm_usage_event_id,
                     query_request_id, input_tokens, output_tokens, latency_ms,
                     estimated_cost, actual_cost, created_at)
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    receipt.id,
                    receipt.tenant_id,
                    receipt.data_source_id,
                    receipt.actor_id,
                    receipt.provider_id,
                    receipt.model_id,
                    receipt.purpose,
                    receipt.privacy_mode,
                    receipt.provider_policy_id,
                    receipt.policy_scope,
                    receipt.provider_policy_version,
                    receipt.declared_classification.value,
                    receipt.detected_classification.value,
                    receipt.effective_classification.value,
                    receipt.maximum_allowed_classification.value,
                    json.dumps(receipt.detection_reason_codes),
                    json.dumps(content_counts, sort_keys=True),
                    receipt.preflight_digest,
                    receipt.confirmation_outcome,
                    receipt.provider_invoked,
                    receipt.decision_code,
                    receipt.llm_usage_event_id,
                    receipt.query_request_id,
                    receipt.input_tokens,
                    receipt.output_tokens,
                    receipt.latency_ms,
                    receipt.estimated_cost,
                    receipt.actual_cost,
                    receipt.created_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=receipt.tenant_id,
                    event_type="ai_transfer.receipt_recorded",
                    subject_type="ai_transfer_receipt",
                    subject_id=receipt.id,
                    details={
                        "data_source_id": receipt.data_source_id,
                        "actor_id": receipt.actor_id,
                        "provider_id": receipt.provider_id,
                        "model_id": receipt.model_id,
                        "purpose": receipt.purpose,
                        "privacy_mode": receipt.privacy_mode,
                        "policy_scope": receipt.policy_scope,
                        "provider_policy_id": receipt.provider_policy_id,
                        "declared_classification": receipt.declared_classification.value,
                        "detected_classification": receipt.detected_classification.value,
                        "effective_classification": receipt.effective_classification.value,
                        "maximum_allowed_classification": (
                            receipt.maximum_allowed_classification.value
                        ),
                        "detection_reason_codes": receipt.detection_reason_codes,
                        "content_counts": content_counts,
                        "preflight_digest": receipt.preflight_digest,
                        "confirmation_outcome": receipt.confirmation_outcome,
                        "provider_invoked": receipt.provider_invoked,
                        "decision_code": receipt.decision_code,
                        "llm_usage_event_id": receipt.llm_usage_event_id,
                        "query_request_id": receipt.query_request_id,
                        "input_tokens": receipt.input_tokens,
                        "output_tokens": receipt.output_tokens,
                        "latency_ms": receipt.latency_ms,
                        "estimated_cost": receipt.estimated_cost,
                        "actual_cost": receipt.actual_cost,
                    },
                )
            )
        return receipt

    @_locked
    def list_ai_transfer_receipts(
        self,
        tenant_id: str,
        *,
        data_source_id: str | None = None,
        limit: int = 100,
    ) -> tuple[AITransferReceipt, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("AI transfer receipt limit must be between 1 and 1000")
        if data_source_id is None:
            rows = self._connection.execute(
                """
                SELECT * FROM ai_transfer_receipts
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM ai_transfer_receipts
                WHERE tenant_id = ? AND data_source_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (tenant_id, data_source_id, limit),
            ).fetchall()
        return tuple(_ai_transfer_receipt_from_row(row) for row in rows)

    @_locked
    def register_preflight_confirmation(
        self,
        token_id: str,
        expires_at: datetime,
    ) -> None:
        if not token_id:
            raise ValueError("Preflight confirmation id is required")
        created_at = utc_now()
        with self._connection:
            self._connection.execute(
                "DELETE FROM ai_preflight_confirmations WHERE expires_at < ?",
                (_utc_iso(created_at),),
            )
            self._connection.execute(
                """
                INSERT INTO ai_preflight_confirmations
                    (token_id, expires_at, consumed_at, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                (token_id, _utc_iso(expires_at), _utc_iso(created_at)),
            )

    @_locked
    def consume_preflight_confirmation(
        self,
        token_id: str,
        consumed_at: datetime,
    ) -> bool:
        if not token_id:
            return False
        consumed_at_iso = _utc_iso(consumed_at)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE ai_preflight_confirmations
                SET consumed_at = ?
                WHERE token_id = ?
                  AND consumed_at IS NULL
                  AND expires_at >= ?
                """,
                (consumed_at_iso, token_id, consumed_at_iso),
            )
        return int(cursor.rowcount) == 1

    @_locked
    def create_model_pricing(self, pricing: ModelPricing) -> ModelPricing:
        self._require_tenant(pricing.tenant_id)
        overlap = self._connection.execute(
            """
            SELECT 1 FROM model_pricing
            WHERE tenant_id = ? AND provider_id = ? AND model_id = ?
              AND valid_from < ?
              AND (valid_to IS NULL OR valid_to > ?)
            LIMIT 1
            """,
            (
                pricing.tenant_id,
                pricing.provider_id,
                pricing.model_id,
                (
                    _utc_iso(pricing.valid_to)
                    if pricing.valid_to is not None
                    else "9999-12-31T23:59:59+00:00"
                ),
                _utc_iso(pricing.valid_from),
            ),
        ).fetchone()
        if overlap is not None:
            raise ValueError("Model pricing validity intervals must not overlap")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO model_pricing
                    (id, tenant_id, provider_id, model_id, valid_from, valid_to,
                     currency, token_unit, input_price_per_unit,
                     cached_input_price_per_unit, output_price_per_unit,
                     batch_discount, notes, source_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pricing.id,
                    pricing.tenant_id,
                    pricing.provider_id,
                    pricing.model_id,
                    _utc_iso(pricing.valid_from),
                    _utc_iso(pricing.valid_to) if pricing.valid_to is not None else None,
                    pricing.currency,
                    pricing.token_unit,
                    str(pricing.input_price_per_unit),
                    (
                        str(pricing.cached_input_price_per_unit)
                        if pricing.cached_input_price_per_unit is not None
                        else None
                    ),
                    str(pricing.output_price_per_unit),
                    str(pricing.batch_discount),
                    pricing.notes,
                    pricing.source_version,
                    pricing.created_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=pricing.tenant_id,
                    event_type="finops.pricing_created",
                    subject_type="model_pricing",
                    subject_id=pricing.id,
                    details={
                        "provider_id": pricing.provider_id,
                        "model_id": pricing.model_id,
                        "currency": pricing.currency,
                        "source_version": pricing.source_version,
                        "valid_from": _utc_iso(pricing.valid_from),
                    },
                )
            )
        return pricing

    @_locked
    def list_model_pricing(self, tenant_id: str) -> tuple[ModelPricing, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM model_pricing
            WHERE tenant_id = ?
            ORDER BY provider_id, model_id, valid_from DESC
            """,
            (tenant_id,),
        ).fetchall()
        return tuple(_model_pricing_from_row(row) for row in rows)

    @_locked
    def get_effective_model_pricing(
        self,
        tenant_id: str,
        provider_id: str,
        model_id: str,
        at: datetime,
    ) -> ModelPricing | None:
        row = self._connection.execute(
            """
            SELECT * FROM model_pricing
            WHERE tenant_id = ? AND provider_id = ? AND model_id = ?
              AND valid_from <= ?
              AND (valid_to IS NULL OR valid_to > ?)
            ORDER BY valid_from DESC
            LIMIT 1
            """,
            (tenant_id, provider_id, model_id, _utc_iso(at), _utc_iso(at)),
        ).fetchone()
        return _model_pricing_from_row(row) if row is not None else None

    @_locked
    def create_tenant_budget(self, budget: TenantBudget) -> TenantBudget:
        self._require_tenant(budget.tenant_id)
        overlap = self._connection.execute(
            """
            SELECT 1 FROM tenant_budgets
            WHERE tenant_id = ? AND currency = ? AND period = ?
              AND valid_from < ?
              AND (valid_to IS NULL OR valid_to > ?)
            LIMIT 1
            """,
            (
                budget.tenant_id,
                budget.currency,
                budget.period.value,
                (
                    _utc_iso(budget.valid_to)
                    if budget.valid_to is not None
                    else "9999-12-31T23:59:59+00:00"
                ),
                _utc_iso(budget.valid_from),
            ),
        ).fetchone()
        if overlap is not None:
            raise ValueError("Tenant budget validity intervals must not overlap")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO tenant_budgets
                    (id, tenant_id, currency, amount, period,
                     valid_from, valid_to, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    budget.id,
                    budget.tenant_id,
                    budget.currency,
                    str(budget.amount),
                    budget.period.value,
                    _utc_iso(budget.valid_from),
                    _utc_iso(budget.valid_to) if budget.valid_to is not None else None,
                    budget.created_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=budget.tenant_id,
                    event_type="finops.budget_created",
                    subject_type="tenant_budget",
                    subject_id=budget.id,
                    details={
                        "currency": budget.currency,
                        "period": budget.period.value,
                        "valid_from": _utc_iso(budget.valid_from),
                    },
                )
            )
        return budget

    @_locked
    def list_tenant_budgets(self, tenant_id: str) -> tuple[TenantBudget, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM tenant_budgets
            WHERE tenant_id = ?
            ORDER BY currency, valid_from DESC
            """,
            (tenant_id,),
        ).fetchall()
        return tuple(_tenant_budget_from_row(row) for row in rows)

    @_locked
    def get_effective_tenant_budget(
        self,
        tenant_id: str,
        currency: str,
        at: datetime,
    ) -> TenantBudget | None:
        row = self._connection.execute(
            """
            SELECT * FROM tenant_budgets
            WHERE tenant_id = ? AND currency = ?
              AND valid_from <= ?
              AND (valid_to IS NULL OR valid_to > ?)
            ORDER BY valid_from DESC
            LIMIT 1
            """,
            (tenant_id, currency, _utc_iso(at), _utc_iso(at)),
        ).fetchone()
        return _tenant_budget_from_row(row) if row is not None else None

    @_locked
    def upsert_execution_cost_policy(
        self,
        policy: ExecutionCostPolicy,
    ) -> ExecutionCostPolicy:
        if self.get_data_source(policy.tenant_id, policy.data_source_id) is None:
            raise LookupError("DataSource does not exist in this tenant")
        row = self._connection.execute(
            """
            SELECT id FROM execution_cost_policies
            WHERE tenant_id = ? AND data_source_id = ?
            """,
            (policy.tenant_id, policy.data_source_id),
        ).fetchone()
        stored = replace(policy, id=row["id"]) if row is not None else policy
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO execution_cost_policies
                    (id, tenant_id, data_source_id, max_total_cost,
                     max_estimated_rows, require_explain, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, data_source_id) DO UPDATE SET
                    max_total_cost = excluded.max_total_cost,
                    max_estimated_rows = excluded.max_estimated_rows,
                    require_explain = excluded.require_explain,
                    updated_at = excluded.updated_at
                """,
                (
                    stored.id,
                    stored.tenant_id,
                    stored.data_source_id,
                    stored.max_total_cost,
                    stored.max_estimated_rows,
                    stored.require_explain,
                    stored.updated_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=stored.tenant_id,
                    event_type="finops.execution_policy_upserted",
                    subject_type="data_source",
                    subject_id=stored.data_source_id,
                    details={
                        "max_total_cost": stored.max_total_cost,
                        "max_estimated_rows": stored.max_estimated_rows,
                        "require_explain": stored.require_explain,
                    },
                )
            )
        return stored

    @_locked
    def get_execution_cost_policy(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> ExecutionCostPolicy | None:
        row = self._connection.execute(
            """
            SELECT * FROM execution_cost_policies
            WHERE tenant_id = ? AND data_source_id = ?
            """,
            (tenant_id, data_source_id),
        ).fetchone()
        return _execution_cost_policy_from_row(row) if row is not None else None

    @_locked
    def upsert_provider_egress_policy(
        self,
        policy: ProviderEgressPolicy,
    ) -> ProviderEgressPolicy:
        self._require_tenant(policy.tenant_id)
        if policy.data_source_id is not None and self.get_data_source(
            policy.tenant_id,
            policy.data_source_id,
        ) is None:
            raise LookupError("DataSource does not exist in this tenant")
        conflict_target = (
            "(tenant_id, provider_id) WHERE data_source_id IS NULL"
            if policy.data_source_id is None
            else "(tenant_id, data_source_id, provider_id) WHERE data_source_id IS NOT NULL"
        )
        with self._connection:
            row = self._connection.execute(
                f"""
                INSERT INTO provider_egress_policies
                    (id, tenant_id, data_source_id, provider_id, allowed,
                     maximum_classification, allowed_purposes_json, data_residency,
                     retention_mode, acknowledgement_digest, acknowledged_by,
                     acknowledged_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT {conflict_target} DO UPDATE SET
                    allowed = excluded.allowed,
                    maximum_classification = excluded.maximum_classification,
                    allowed_purposes_json = excluded.allowed_purposes_json,
                    data_residency = excluded.data_residency,
                    retention_mode = excluded.retention_mode,
                    acknowledgement_digest = excluded.acknowledgement_digest,
                    acknowledged_by = excluded.acknowledged_by,
                    acknowledged_at = excluded.acknowledged_at,
                    updated_at = excluded.updated_at
                RETURNING *
                """,
                (
                    policy.id,
                    policy.tenant_id,
                    policy.data_source_id,
                    policy.provider_id,
                    policy.allowed,
                    policy.maximum_classification.value,
                    json.dumps(policy.allowed_purposes),
                    policy.data_residency,
                    policy.retention_mode.value,
                    policy.acknowledgement_digest,
                    policy.acknowledged_by,
                    (
                        policy.acknowledged_at.isoformat()
                        if policy.acknowledged_at is not None
                        else None
                    ),
                    policy.updated_at.isoformat(),
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("Provider egress policy upsert returned no row")
            stored = _provider_egress_policy_from_row(row)
            self._append_audit(
                AuditEvent(
                    tenant_id=stored.tenant_id,
                    event_type="provider_egress_policy.upserted",
                    subject_type="provider_egress_policy",
                    subject_id=stored.id,
                    details={
                        "provider_id": stored.provider_id,
                        "data_source_id": stored.data_source_id,
                        "allowed": stored.allowed,
                        "maximum_classification": stored.maximum_classification.value,
                        "allowed_purposes": stored.allowed_purposes,
                        "data_residency": stored.data_residency,
                        "retention_mode": stored.retention_mode.value,
                        "acknowledged": stored.acknowledgement_digest is not None,
                        "acknowledged_by": stored.acknowledged_by,
                    },
                )
            )
        return stored

    @_locked
    def get_effective_provider_egress_policy(
        self,
        tenant_id: str,
        provider_id: str,
        data_source_id: str | None,
    ) -> ProviderEgressPolicy | None:
        if data_source_id is None:
            row = self._connection.execute(
                """
                SELECT * FROM provider_egress_policies
                WHERE tenant_id = ? AND provider_id = ? AND data_source_id IS NULL
                """,
                (tenant_id, provider_id),
            ).fetchone()
        else:
            row = self._connection.execute(
                """
                SELECT * FROM provider_egress_policies
                WHERE tenant_id = ? AND provider_id = ?
                  AND (data_source_id = ? OR data_source_id IS NULL)
                ORDER BY CASE WHEN data_source_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (tenant_id, provider_id, data_source_id, data_source_id),
            ).fetchone()
        return _provider_egress_policy_from_row(row) if row is not None else None

    @_locked
    def list_provider_egress_policies(
        self,
        tenant_id: str,
    ) -> tuple[ProviderEgressPolicy, ...]:
        self._require_tenant(tenant_id)
        rows = self._connection.execute(
            """
            SELECT * FROM provider_egress_policies
            WHERE tenant_id = ?
            ORDER BY provider_id, data_source_id, id
            """,
            (tenant_id,),
        ).fetchall()
        return tuple(_provider_egress_policy_from_row(row) for row in rows)

    @_locked
    def try_acquire_request_quota(
        self,
        *,
        scope_key: str,
        window_number: int,
        max_requests: int,
        max_concurrent: int,
        updated_at: datetime,
    ) -> tuple[bool, str | None]:
        if not scope_key or window_number < 0 or max_requests < 1 or max_concurrent < 1:
            raise ValueError("Invalid request quota acquisition")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO request_quota_windows
                    (scope_key, window_number, request_count, active_requests, updated_at)
                VALUES (?, ?, 0, 0, ?)
                ON CONFLICT (scope_key) DO NOTHING
                """,
                (scope_key, window_number, updated_at.isoformat()),
            )
            cursor = self._connection.execute(
                """
                UPDATE request_quota_windows
                SET window_number = ?,
                    request_count = CASE
                        WHEN window_number = ? THEN request_count + 1
                        ELSE 1
                    END,
                    active_requests = CASE
                        WHEN window_number = ? THEN active_requests + 1
                        ELSE 1
                    END,
                    updated_at = ?
                WHERE scope_key = ?
                  AND (window_number <> ? OR active_requests < ?)
                  AND (window_number <> ? OR request_count < ?)
                """,
                (
                    window_number,
                    window_number,
                    window_number,
                    updated_at.isoformat(),
                    scope_key,
                    window_number,
                    max_concurrent,
                    window_number,
                    max_requests,
                ),
            )
            if cursor.rowcount == 1:
                return True, None
            row = self._connection.execute(
                """
                SELECT window_number, request_count, active_requests
                FROM request_quota_windows WHERE scope_key = ?
                """,
                (scope_key,),
            ).fetchone()
            if row is not None and int(row["active_requests"]) >= max_concurrent:
                return False, "concurrency"
            return False, "rate"

    @_locked
    def release_request_quota(
        self,
        scope_key: str,
        window_number: int,
        updated_at: datetime,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE request_quota_windows
                SET active_requests = CASE
                        WHEN active_requests > 0 THEN active_requests - 1
                        ELSE 0
                    END,
                    updated_at = ?
                WHERE scope_key = ? AND window_number = ?
                """,
                (updated_at.isoformat(), scope_key, window_number),
            )

    @_locked
    def enqueue_background_job(self, job: BackgroundJob) -> BackgroundJob:
        self._require_tenant(job.tenant_id)
        if job.data_source_id is not None and self.get_data_source(
            job.tenant_id, job.data_source_id
        ) is None:
            raise LookupError("DataSource does not exist in this tenant")
        with self._connection:
            return self._enqueue_background_job_on_connection(job)

    def _enqueue_background_job_on_connection(
        self,
        job: BackgroundJob,
    ) -> BackgroundJob:
        cursor = self._connection.execute(
            """
            INSERT INTO background_jobs
                (id, tenant_id, data_source_id, job_type, payload_json, status,
                 attempt_count, max_attempts, scheduled_at, lease_expires_at,
                 worker_id, result_json, last_error_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                job.id,
                job.tenant_id,
                job.data_source_id,
                job.job_type,
                json.dumps(dict(job.payload), sort_keys=True),
                job.status.value,
                job.attempt_count,
                job.max_attempts,
                job.scheduled_at.isoformat(),
                (
                    job.lease_expires_at.isoformat()
                    if job.lease_expires_at is not None
                    else None
                ),
                job.worker_id,
                json.dumps(dict(job.result), sort_keys=True)
                if job.result is not None
                else None,
                job.last_error_code,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
            ),
        )
        if cursor.rowcount == 0:
            existing = self._connection.execute(
                """
                SELECT * FROM background_jobs
                WHERE tenant_id = ? AND job_type = ?
                  AND ((data_source_id = ?) OR (data_source_id IS NULL AND ? IS NULL))
                  AND status IN ('queued', 'running')
                ORDER BY created_at, id LIMIT 1
                """,
                (
                    job.tenant_id,
                    job.job_type,
                    job.data_source_id,
                    job.data_source_id,
                ),
            ).fetchone()
            if existing is None:
                raise RuntimeError("Background job enqueue conflict could not be resolved")
            return _background_job_from_row(existing)
        self._append_audit(
            AuditEvent(
                tenant_id=job.tenant_id,
                event_type="background_job.queued",
                subject_type="background_job",
                subject_id=job.id,
                details={
                    "job_type": job.job_type,
                    "data_source_id": job.data_source_id,
                },
            )
        )
        return job

    @_locked
    def claim_background_job(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> BackgroundJob | None:
        if not worker_id.strip() or not 5 <= lease_seconds <= 3600:
            raise ValueError("Invalid background job lease")
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._connection:
            row = self._connection.execute(
                """
                UPDATE background_jobs
                SET status = 'running', worker_id = ?, attempt_count = attempt_count + 1,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = (
                    SELECT id FROM background_jobs
                    WHERE scheduled_at <= ?
                      AND (
                        status = 'queued'
                        OR (status = 'running' AND lease_expires_at < ?)
                      )
                    ORDER BY scheduled_at, created_at, id
                    LIMIT 1
                )
                  AND (
                    status = 'queued'
                    OR (status = 'running' AND lease_expires_at < ?)
                  )
                RETURNING *
                """,
                (
                    worker_id,
                    lease_expires_at.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            ).fetchone()
        return _background_job_from_row(row) if row is not None else None

    @_locked
    def heartbeat_background_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE background_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (lease_expires_at.isoformat(), now.isoformat(), job_id, worker_id),
            )
        return cursor.rowcount == 1

    @_locked
    def complete_background_job(
        self,
        job_id: str,
        worker_id: str,
        result: Mapping[str, object],
        *,
        now: datetime,
        continuation_payload: Mapping[str, object] | None = None,
    ) -> BackgroundJob:
        with self._connection:
            row = self._connection.execute(
                """
                UPDATE background_jobs
                SET status = 'succeeded', result_json = ?, last_error_code = NULL,
                    lease_expires_at = NULL, worker_id = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND worker_id = ?
                RETURNING *
                """,
                (json.dumps(dict(result), sort_keys=True), now.isoformat(), job_id, worker_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Background job lease is no longer owned by this worker")
            completed = _background_job_from_row(row)
            self._append_audit(
                AuditEvent(
                    tenant_id=completed.tenant_id,
                    event_type="background_job.succeeded",
                    subject_type="background_job",
                    subject_id=completed.id,
                    details={
                        "job_type": completed.job_type,
                        "attempt_count": completed.attempt_count,
                    },
                )
            )
            if continuation_payload is not None:
                self._enqueue_background_job_on_connection(
                    BackgroundJob(
                        tenant_id=completed.tenant_id,
                        data_source_id=completed.data_source_id,
                        job_type=completed.job_type,
                        payload=continuation_payload,
                        max_attempts=completed.max_attempts,
                        scheduled_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return completed

    @_locked
    def fail_background_job(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        *,
        now: datetime,
        retry_delay_seconds: int,
    ) -> BackgroundJob:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,199}", error_code):
            raise ValueError("Background job error code is invalid")
        if not 0 <= retry_delay_seconds <= 3600:
            raise ValueError("Background job retry delay is invalid")
        with self._connection:
            row = self._connection.execute(
                """
                UPDATE background_jobs
                SET status = CASE WHEN attempt_count < max_attempts THEN 'queued' ELSE 'failed' END,
                    scheduled_at = CASE
                        WHEN attempt_count < max_attempts THEN ? ELSE scheduled_at
                    END,
                    result_json = NULL, last_error_code = ?, lease_expires_at = NULL,
                    worker_id = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND worker_id = ?
                RETURNING *
                """,
                (
                    (now + timedelta(seconds=retry_delay_seconds)).isoformat(),
                    error_code,
                    now.isoformat(),
                    job_id,
                    worker_id,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("Background job lease is no longer owned by this worker")
            failed = _background_job_from_row(row)
            self._append_audit(
                AuditEvent(
                    tenant_id=failed.tenant_id,
                    event_type=(
                        "background_job.retry_scheduled"
                        if failed.status is BackgroundJobStatus.QUEUED
                        else "background_job.failed"
                    ),
                    subject_type="background_job",
                    subject_id=failed.id,
                    details={
                        "job_type": failed.job_type,
                        "attempt_count": failed.attempt_count,
                        "error_code": failed.last_error_code,
                    },
                )
            )
        return failed

    @_locked
    def cancel_background_job(
        self,
        tenant_id: str,
        job_id: str,
        *,
        now: datetime,
    ) -> BackgroundJob:
        with self._connection:
            row = self._connection.execute(
                """
                UPDATE background_jobs
                SET status = 'cancelled', lease_expires_at = NULL, worker_id = NULL,
                    updated_at = ?
                WHERE tenant_id = ? AND id = ? AND status = 'queued'
                RETURNING *
                """,
                (now.isoformat(), tenant_id, job_id),
            ).fetchone()
            if row is None:
                raise LookupError("Queued background job not found")
            cancelled = _background_job_from_row(row)
            self._append_audit(
                AuditEvent(
                    tenant_id=tenant_id,
                    event_type="background_job.cancelled",
                    subject_type="background_job",
                    subject_id=job_id,
                    details={"job_type": cancelled.job_type},
                )
            )
        return cancelled

    @_locked
    def get_background_job(self, tenant_id: str, job_id: str) -> BackgroundJob | None:
        row = self._connection.execute(
            "SELECT * FROM background_jobs WHERE tenant_id = ? AND id = ?",
            (tenant_id, job_id),
        ).fetchone()
        return _background_job_from_row(row) if row is not None else None

    @_locked
    def list_background_jobs(
        self,
        tenant_id: str,
        *,
        data_source_id: str | None = None,
        limit: int = 100,
    ) -> tuple[BackgroundJob, ...]:
        self._require_tenant(tenant_id)
        if not 1 <= limit <= 500:
            raise ValueError("Background job list limit is invalid")
        rows = self._connection.execute(
            """
            SELECT * FROM background_jobs
            WHERE tenant_id = ? AND (? IS NULL OR data_source_id = ?)
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (tenant_id, data_source_id, data_source_id, limit),
        ).fetchall()
        return tuple(_background_job_from_row(row) for row in rows)

    @_locked
    def preview_operational_retention(
        self,
        cutoff: datetime,
    ) -> OperationalRetentionReport:
        _validate_retention_cutoff(cutoff)
        jobs_row = self._connection.execute(
            """
            SELECT COUNT(*) AS item_count FROM background_jobs
            WHERE status IN ('succeeded', 'failed', 'cancelled') AND updated_at < ?
            """,
            (cutoff.isoformat(),),
        ).fetchone()
        quota_row = self._connection.execute(
            """
            SELECT COUNT(*) AS item_count FROM request_quota_windows
            WHERE active_requests = 0 AND updated_at < ?
            """,
            (cutoff.isoformat(),),
        ).fetchone()
        return OperationalRetentionReport(
            cutoff=cutoff,
            background_jobs=int(jobs_row["item_count"]) if jobs_row is not None else 0,
            quota_windows=int(quota_row["item_count"]) if quota_row is not None else 0,
        )

    @_locked
    def purge_operational_records(
        self,
        cutoff: datetime,
        *,
        actor_id: str,
    ) -> OperationalRetentionReport:
        _validate_retention_cutoff(cutoff)
        normalized_actor_id = actor_id.strip()
        if not normalized_actor_id or len(normalized_actor_id) > 200:
            raise ValueError("Retention actor ID is invalid")
        completed_at = utc_now()
        run_id = str(uuid4())
        with self._connection:
            jobs_cursor = self._connection.execute(
                """
                DELETE FROM background_jobs
                WHERE status IN ('succeeded', 'failed', 'cancelled') AND updated_at < ?
                """,
                (cutoff.isoformat(),),
            )
            quota_cursor = self._connection.execute(
                """
                DELETE FROM request_quota_windows
                WHERE active_requests = 0 AND updated_at < ?
                """,
                (cutoff.isoformat(),),
            )
            self._connection.execute(
                """
                INSERT INTO operational_retention_runs
                    (id, cutoff, background_jobs_deleted, quota_windows_deleted,
                     actor_id, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    cutoff.isoformat(),
                    jobs_cursor.rowcount,
                    quota_cursor.rowcount,
                    normalized_actor_id,
                    completed_at.isoformat(),
                ),
            )
        return OperationalRetentionReport(
            cutoff=cutoff,
            background_jobs=jobs_cursor.rowcount,
            quota_windows=quota_cursor.rowcount,
            run_id=run_id,
            actor_id=normalized_actor_id,
            completed_at=completed_at,
        )

    @_locked
    def create_authorized_query_definition(
        self,
        definition: AuthorizedQueryDefinition,
    ) -> AuthorizedQueryDefinition:
        data_source = self.get_data_source(
            definition.tenant_id,
            definition.data_source_id,
        )
        if data_source is None:
            raise LookupError("DataSource does not exist in this tenant")
        if data_source.source_type is not DataSourceType.AUTHORIZED_QUERY:
            raise ValueError("DataSource is not an authorized query source")
        self._require_catalog_version(
            definition.tenant_id,
            definition.catalog_version_id,
            definition.data_source_id,
        )
        object_row = self._connection.execute(
            """
            SELECT object_kind FROM schema_objects
            WHERE tenant_id = ? AND catalog_version_id = ?
              AND schema_name = ? AND object_name = ?
            """,
            (
                definition.tenant_id,
                definition.catalog_version_id,
                definition.virtual_schema,
                definition.virtual_name,
            ),
        ).fetchone()
        if object_row is None or (
            ObjectKind(object_row["object_kind"]) is not ObjectKind.VIRTUAL_QUERY
        ):
            raise ValueError(
                "Authorized query definition must match a virtual object in its catalog version"
            )
        version_row = self._connection.execute(
            """
            SELECT COALESCE(MAX(definition_version), 0) + 1 AS expected_version
            FROM authorized_query_definitions
            WHERE tenant_id = ? AND data_source_id = ?
            """,
            (definition.tenant_id, definition.data_source_id),
        ).fetchone()
        if definition.version != int(version_row["expected_version"]):
            raise ValueError("Authorized query definition version changed concurrently")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO authorized_query_definitions
                    (id, tenant_id, data_source_id, catalog_version_id,
                     definition_version, virtual_schema, virtual_name, description,
                     base_sql, normalized_base_sql, parameters_json,
                     allow_filtering, allow_aggregation, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition.id,
                    definition.tenant_id,
                    definition.data_source_id,
                    definition.catalog_version_id,
                    definition.version,
                    definition.virtual_schema,
                    definition.virtual_name,
                    definition.description,
                    definition.base_sql,
                    definition.normalized_base_sql,
                    json.dumps(
                        [
                            {
                                "name": parameter.name,
                                "physical_type": parameter.physical_type,
                                "nullable": parameter.nullable,
                            }
                            for parameter in definition.parameters
                        ],
                        sort_keys=True,
                    ),
                    definition.allow_filtering,
                    definition.allow_aggregation,
                    definition.created_at.isoformat(),
                ),
            )
            self._append_audit(
                AuditEvent(
                    tenant_id=definition.tenant_id,
                    event_type="authorized_query.definition_created",
                    subject_type="authorized_query_definition",
                    subject_id=definition.id,
                    details={
                        "data_source_id": definition.data_source_id,
                        "catalog_version_id": definition.catalog_version_id,
                        "definition_version": definition.version,
                        "virtual_object_ref": definition.virtual_object_ref,
                        "parameter_names": tuple(
                            parameter.name for parameter in definition.parameters
                        ),
                        "allow_filtering": definition.allow_filtering,
                        "allow_aggregation": definition.allow_aggregation,
                    },
                )
            )
        return definition

    @_locked
    def get_authorized_query_definition(
        self,
        tenant_id: str,
        data_source_id: str,
        catalog_version_id: str,
    ) -> AuthorizedQueryDefinition | None:
        row = self._connection.execute(
            """
            SELECT * FROM authorized_query_definitions
            WHERE tenant_id = ? AND data_source_id = ? AND catalog_version_id = ?
            """,
            (tenant_id, data_source_id, catalog_version_id),
        ).fetchone()
        return _authorized_query_definition_from_row(row) if row is not None else None

    @_locked
    def get_latest_authorized_query_definition(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> AuthorizedQueryDefinition | None:
        row = self._connection.execute(
            """
            SELECT * FROM authorized_query_definitions
            WHERE tenant_id = ? AND data_source_id = ?
            ORDER BY definition_version DESC
            LIMIT 1
            """,
            (tenant_id, data_source_id),
        ).fetchone()
        return _authorized_query_definition_from_row(row) if row is not None else None

    @_locked
    def list_authorized_query_definitions(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[AuthorizedQueryDefinition, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM authorized_query_definitions
            WHERE tenant_id = ? AND data_source_id = ?
            ORDER BY definition_version DESC
            """,
            (tenant_id, data_source_id),
        ).fetchall()
        return tuple(_authorized_query_definition_from_row(row) for row in rows)

    def _upsert_resolution(self, resolution: SemanticResolution) -> None:
        self._connection.execute(
            """
            INSERT INTO semantic_resolutions
                (tenant_id, data_source_id, object_ref, description, epistemic_status, confidence,
                 selected_definition_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, data_source_id, object_ref) DO UPDATE SET
                description = excluded.description,
                epistemic_status = excluded.epistemic_status,
                confidence = excluded.confidence,
                selected_definition_id = excluded.selected_definition_id,
                updated_at = excluded.updated_at
            """,
            (
                resolution.tenant_id,
                resolution.data_source_id,
                resolution.object_ref,
                resolution.description,
                resolution.status.value,
                resolution.confidence,
                resolution.selected_definition_id,
                resolution.updated_at.isoformat(),
            ),
        )

    def _upsert_business_concept_resolution(
        self,
        resolution: BusinessConceptResolution,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO business_concept_resolutions
                (tenant_id, data_source_id, concept_key, concept_name, description,
                 synonyms_json, object_refs_json, content_classification,
                 epistemic_status, confidence, selected_definition_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, data_source_id, concept_key) DO UPDATE SET
                concept_name = excluded.concept_name,
                description = excluded.description,
                synonyms_json = excluded.synonyms_json,
                object_refs_json = excluded.object_refs_json,
                content_classification = excluded.content_classification,
                epistemic_status = excluded.epistemic_status,
                confidence = excluded.confidence,
                selected_definition_id = excluded.selected_definition_id,
                updated_at = excluded.updated_at
            """,
            (
                resolution.tenant_id,
                resolution.data_source_id,
                resolution.concept_key,
                resolution.name,
                resolution.description,
                json.dumps(resolution.synonyms),
                json.dumps(resolution.object_refs),
                resolution.content_classification.value,
                resolution.status.value,
                resolution.confidence,
                resolution.selected_definition_id,
                resolution.updated_at.isoformat(),
            ),
        )

    def _upsert_analytic_semantic_resolution(
        self,
        resolution: AnalyticSemanticResolution,
    ) -> None:
        kind, asset_key = _analytic_kind_and_key(resolution)
        self._connection.execute(
            """
            INSERT INTO analytic_semantic_resolutions
                (tenant_id, data_source_id, asset_kind, asset_key, payload_json,
                 content_classification, epistemic_status, confidence,
                 selected_definition_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, data_source_id, asset_kind, asset_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                content_classification = excluded.content_classification,
                epistemic_status = excluded.epistemic_status,
                confidence = excluded.confidence,
                selected_definition_id = excluded.selected_definition_id,
                updated_at = excluded.updated_at
            """,
            (
                resolution.tenant_id,
                resolution.data_source_id,
                kind.value,
                asset_key,
                json.dumps(_analytic_payload(resolution), sort_keys=True),
                resolution.content_classification.value,
                resolution.status.value,
                resolution.confidence,
                resolution.selected_definition_id,
                resolution.updated_at.isoformat(),
            ),
        )

    def _append_audit(self, event: AuditEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO audit_events
                (id, tenant_id, event_type, subject_type, subject_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.tenant_id,
                event.event_type,
                event.subject_type,
                event.subject_id,
                json.dumps(dict(event.details), sort_keys=True),
                event.created_at.isoformat(),
            ),
        )

    def _require_tenant(self, tenant_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise LookupError("Tenant does not exist")

    def _require_catalog_version(
        self,
        tenant_id: str,
        catalog_version_id: str,
        data_source_id: str | None = None,
    ) -> None:
        row = self._connection.execute(
            """
            SELECT 1 FROM catalog_versions
            WHERE tenant_id = ? AND id = ? AND (? IS NULL OR data_source_id = ?)
            """,
            (tenant_id, catalog_version_id, data_source_id, data_source_id),
        ).fetchone()
        if row is None:
            raise LookupError("Catalog version does not exist in this tenant")

    def _require_schema_object(self, tenant_id: str, schema_object_id: str) -> None:
        row = self._connection.execute(
            """
            SELECT 1 FROM schema_objects
            WHERE tenant_id = ? AND id = ?
            """,
            (tenant_id, schema_object_id),
        ).fetchone()
        if row is None:
            raise LookupError("Schema object does not exist in this tenant")

    def _catalog_version_data_source(
        self,
        tenant_id: str,
        catalog_version_id: str,
    ) -> str:
        row = self._connection.execute(
            """
            SELECT data_source_id FROM catalog_versions
            WHERE tenant_id = ? AND id = ?
            """,
            (tenant_id, catalog_version_id),
        ).fetchone()
        if row is None:
            raise LookupError("Catalog version does not exist in this tenant")
        return str(row["data_source_id"])

    def _upgrade_legacy_schema(self) -> None:
        """Keep pre-alpha SQLite catalogs usable while the first migration is evolving."""

        additions = {
            "data_sources": {"connection_secret_ref": "TEXT"},
            "schema_objects": {"definition_sql": "TEXT"},
            "column_definitions": {
                "default_expression": "TEXT",
                "is_primary_key": "INTEGER NOT NULL DEFAULT 0",
            },
            "semantic_definitions": {
                "actor_id": "TEXT",
                "reason": "TEXT",
            },
            "llm_usage_events": {
                "cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "currency": "TEXT",
                "pricing_id": "TEXT",
            },
            "query_requests": {
                "business_concepts_json": "TEXT NOT NULL DEFAULT '[]'",
                "metrics_json": "TEXT NOT NULL DEFAULT '[]'",
                "business_rules_json": "TEXT NOT NULL DEFAULT '[]'",
                "assumptions_json": "TEXT NOT NULL DEFAULT '[]'",
                "provider_id": "TEXT",
                "model_id": "TEXT",
                "llm_usage_event_id": "TEXT",
                "estimated_db_cost": "REAL",
                "estimated_db_rows": "INTEGER",
                "explained_at": "TEXT",
                "parameter_names_json": "TEXT NOT NULL DEFAULT '[]'",
                "parameter_value_hash": "TEXT",
                "parameter_definitions_json": "TEXT NOT NULL DEFAULT '[]'",
                "output_lineage_json": "TEXT NOT NULL DEFAULT '[]'",
                "output_lineage_complete": "INTEGER NOT NULL DEFAULT 0",
            },
            "provider_egress_policies": {
                "acknowledgement_digest": "TEXT",
                "acknowledged_by": "TEXT",
                "acknowledged_at": "TEXT",
            },
        }
        for table_name, columns in additions.items():
            existing = {
                row["name"]
                for row in self._connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            for column_name, declaration in columns.items():
                if column_name not in existing:
                    self._connection.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
                    )
        resolution_columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(semantic_resolutions)")
        }
        if "data_source_id" not in resolution_columns:
            self._upgrade_semantic_resolution_scope()

    def _upgrade_semantic_resolution_scope(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self._connection.executescript(
                """
                BEGIN;
                CREATE TABLE semantic_resolutions_v2 (
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    data_source_id TEXT NOT NULL,
                    object_ref TEXT NOT NULL,
                    description TEXT NOT NULL,
                    epistemic_status TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    selected_definition_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, data_source_id, object_ref),
                    FOREIGN KEY (tenant_id, data_source_id)
                        REFERENCES data_sources(tenant_id, id),
                    FOREIGN KEY (tenant_id, selected_definition_id)
                        REFERENCES semantic_definitions(tenant_id, id)
                );
                INSERT INTO semantic_resolutions_v2
                    (tenant_id, data_source_id, object_ref, description, epistemic_status,
                     confidence, selected_definition_id, updated_at)
                SELECT
                    resolution.tenant_id,
                    COALESCE(
                        (
                            SELECT version.data_source_id
                            FROM semantic_definitions AS definition
                            JOIN catalog_versions AS version
                              ON version.tenant_id = definition.tenant_id
                             AND version.id = definition.catalog_version_id
                            WHERE definition.tenant_id = resolution.tenant_id
                              AND definition.id = resolution.selected_definition_id
                        ),
                        (
                            SELECT version.data_source_id
                            FROM semantic_definitions AS definition
                            JOIN catalog_versions AS version
                              ON version.tenant_id = definition.tenant_id
                             AND version.id = definition.catalog_version_id
                            WHERE definition.tenant_id = resolution.tenant_id
                              AND definition.object_ref = resolution.object_ref
                            ORDER BY definition.created_at DESC, definition.id DESC
                            LIMIT 1
                        )
                    ),
                    resolution.object_ref,
                    resolution.description,
                    resolution.epistemic_status,
                    resolution.confidence,
                    resolution.selected_definition_id,
                    resolution.updated_at
                FROM semantic_resolutions AS resolution
                WHERE EXISTS (
                    SELECT 1
                    FROM semantic_definitions AS definition
                    WHERE definition.tenant_id = resolution.tenant_id
                      AND definition.object_ref = resolution.object_ref
                );
                DROP TABLE semantic_resolutions;
                ALTER TABLE semantic_resolutions_v2 RENAME TO semantic_resolutions;
                COMMIT;
                """
            )
        finally:
            self._connection.execute("PRAGMA foreign_keys = ON")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _tenant_from_row(row: sqlite3.Row) -> Tenant:
    return Tenant(
        id=row["id"],
        name=row["name"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _data_source_from_row(row: sqlite3.Row) -> DataSource:
    return DataSource(
        id=row["id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        source_type=DataSourceType(row["source_type"]),
        dialect=row["dialect"],
        capabilities=frozenset(
            DataSourceCapability(value)
            for value in json.loads(row["capabilities_json"])
        ),
        connection_secret_ref=row["connection_secret_ref"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _background_job_from_row(row: sqlite3.Row) -> BackgroundJob:
    lease_expires_at = row["lease_expires_at"]
    result_json = row["result_json"]
    return BackgroundJob(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        job_type=row["job_type"],
        payload=json.loads(row["payload_json"]),
        status=BackgroundJobStatus(row["status"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        scheduled_at=_parse_datetime(row["scheduled_at"]),
        lease_expires_at=(
            _parse_datetime(lease_expires_at) if lease_expires_at is not None else None
        ),
        worker_id=row["worker_id"],
        result=json.loads(result_json) if result_json is not None else None,
        last_error_code=row["last_error_code"],
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _query_request_from_row(row: sqlite3.Row) -> QueryRequest:
    approved_at = row["approved_at"]
    explained_at = row["explained_at"]
    return QueryRequest(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        catalog_version_id=row["catalog_version_id"],
        sql_text=row["sql_text"],
        normalized_sql=row["normalized_sql"],
        referenced_tables=tuple(json.loads(row["referenced_tables_json"])),
        referenced_columns=tuple(json.loads(row["referenced_columns_json"])),
        validation_issue_codes=tuple(json.loads(row["validation_issue_codes_json"])),
        state=QueryRequestState(row["state"]),
        business_concepts=tuple(json.loads(row["business_concepts_json"])),
        metrics=tuple(json.loads(row["metrics_json"])),
        business_rules=tuple(json.loads(row["business_rules_json"])),
        assumptions=tuple(json.loads(row["assumptions_json"])),
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        llm_usage_event_id=row["llm_usage_event_id"],
        estimated_db_cost=(
            float(row["estimated_db_cost"])
            if row["estimated_db_cost"] is not None
            else None
        ),
        estimated_db_rows=(
            int(row["estimated_db_rows"])
            if row["estimated_db_rows"] is not None
            else None
        ),
        explained_at=(
            _parse_datetime(explained_at) if explained_at is not None else None
        ),
        parameter_names=tuple(json.loads(row["parameter_names_json"])),
        parameter_value_hash=row["parameter_value_hash"],
        parameter_definitions=tuple(
            QueryParameterDefinition(
                name=item["name"],
                value_type=QueryParameterType(item["value_type"]),
                nullable=bool(item["nullable"]),
            )
            for item in json.loads(row["parameter_definitions_json"])
        ),
        output_lineage=tuple(
            OutputColumnLineage(
                output_name=item["output_name"],
                source_columns=tuple(item["source_columns"]),
            )
            for item in json.loads(row["output_lineage_json"])
        ),
        output_lineage_complete=bool(row["output_lineage_complete"]),
        approved_by=row["approved_by"],
        approved_at=_parse_datetime(approved_at) if approved_at is not None else None,
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _corrected_sql_example_from_row(row: sqlite3.Row) -> CorrectedSQLExample:
    return CorrectedSQLExample(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        catalog_version_id=row["catalog_version_id"],
        question=row["question"],
        normalized_question=row["normalized_question"],
        content_classification=Classification(row["content_classification"]),
        sql_text=row["sql_text"],
        normalized_sql=row["normalized_sql"],
        referenced_tables=tuple(json.loads(row["referenced_tables_json"])),
        referenced_columns=tuple(json.loads(row["referenced_columns_json"])),
        business_concepts=tuple(json.loads(row["business_concepts_json"])),
        assumptions=tuple(json.loads(row["assumptions_json"])),
        actor_id=row["actor_id"],
        reason=row["reason"],
        source_query_request_id=row["source_query_request_id"],
        supersedes_example_id=row["supersedes_example_id"],
        revision=int(row["revision"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _query_feedback_event_from_row(row: sqlite3.Row) -> QueryFeedbackEvent:
    return QueryFeedbackEvent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        query_request_id=row["query_request_id"],
        outcome=QueryFeedbackOutcome(row["outcome"]),
        actor_id=row["actor_id"],
        reason=row["reason"],
        corrected_sql_example_id=row["corrected_sql_example_id"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _golden_evaluation_candidate_from_row(
    row: sqlite3.Row,
) -> GoldenEvaluationCandidate:
    return GoldenEvaluationCandidate(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        catalog_version_id=row["catalog_version_id"],
        corrected_sql_example_id=row["corrected_sql_example_id"],
        source_query_request_id=row["source_query_request_id"],
        question=row["question"],
        normalized_sql=row["normalized_sql"],
        referenced_tables=tuple(json.loads(row["referenced_tables_json"])),
        referenced_columns=tuple(json.loads(row["referenced_columns_json"])),
        business_concepts=tuple(json.loads(row["business_concepts_json"])),
        assumptions=tuple(json.loads(row["assumptions_json"])),
        content_classification=Classification(row["content_classification"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _golden_candidate_review_from_row(row: sqlite3.Row) -> GoldenCandidateReview:
    return GoldenCandidateReview(
        id=row["id"],
        tenant_id=row["tenant_id"],
        candidate_id=row["candidate_id"],
        status=GoldenCandidateStatus(row["decision"]),
        actor_id=row["actor_id"],
        reason=row["reason"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _next_resolution_timestamp(
    current: (
        SemanticResolution
        | BusinessConceptResolution
        | MetricResolution
        | BusinessRuleResolution
        | None
    ),
) -> datetime:
    candidate = utc_now()
    if current is None or candidate > current.updated_at:
        return candidate
    return current.updated_at + timedelta(microseconds=1)


def _require_current_resolution[T](
    current: T | None,
    decision: ResolutionDecision,
) -> T:
    if current is None:
        raise RuntimeError(
            f"Resolution action {decision.action.value} requires an existing resolution"
        )
    return current


def _validate_retention_cutoff(cutoff: datetime) -> None:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("Retention cutoff must be timezone-aware")
    if cutoff >= utc_now():
        raise ValueError("Retention cutoff must be in the past")


_POSTGRESQL_JSON_COLUMNS = {
    "assumptions_json": "assumptions",
    "allowed_purposes_json": "allowed_purposes",
    "business_concepts_json": "business_concepts",
    "business_rules_json": "business_rules",
    "capabilities_json": "capabilities",
    "content_counts_json": "content_counts",
    "detection_reason_codes_json": "detection_reason_codes",
    "details_json": "details",
    "metrics_json": "metrics",
    "object_refs_json": "object_refs",
    "output_lineage_json": "output_lineage",
    "parameter_definitions_json": "parameter_definitions",
    "parameter_names_json": "parameter_names",
    "parameters_json": "parameters",
    "payload_json": "payload",
    "result_json": "result",
    "referenced_columns_json": "referenced_columns",
    "referenced_tables_json": "referenced_tables",
    "source_columns_json": "source_columns",
    "synonyms_json": "synonyms",
    "target_columns_json": "target_columns",
    "validation_issue_codes_json": "validation_issue_codes",
}
_POSTGRESQL_JSON_ALIASES = {
    database_name: repository_name
    for repository_name, database_name in _POSTGRESQL_JSON_COLUMNS.items()
}


class _BufferedPostgreSQLCursor:
    def __init__(
        self,
        rows: tuple[Mapping[str, Any], ...],
        rowcount: int,
    ) -> None:
        self._rows = rows
        self._offset = 0
        self.rowcount = rowcount

    def fetchone(self) -> Mapping[str, Any] | None:
        if self._offset >= len(self._rows):
            return None
        row = self._rows[self._offset]
        self._offset += 1
        return row

    def fetchall(self) -> list[Mapping[str, Any]]:
        rows = list(self._rows[self._offset :])
        self._offset = len(self._rows)
        return rows


class _PostgreSQLConnectionAdapter:
    """Expose the small sqlite connection surface used by the shared repository methods."""

    def __init__(self, pool: Any, integrity_error: type[Exception]) -> None:
        self._pool = pool
        self._integrity_error = integrity_error
        self._active_connection: Any | None = None
        self._connection_context: Any | None = None
        self._context_depth = 0

    def __enter__(self) -> _PostgreSQLConnectionAdapter:
        if self._active_connection is not None:
            self._context_depth += 1
            return self
        context = self._pool.connection()
        self._active_connection = context.__enter__()
        self._connection_context = context
        self._context_depth = 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool:
        self._context_depth -= 1
        if self._context_depth > 0:
            return False
        context = self._connection_context
        self._active_connection = None
        self._connection_context = None
        if context is None:
            return False
        return bool(context.__exit__(exc_type, exc_value, traceback))

    def execute(
        self,
        query: str,
        parameters: Iterable[object] = (),
    ) -> _BufferedPostgreSQLCursor:
        translated_query = _translate_postgresql_query(query)
        translated_parameters = tuple(parameters)
        if self._active_connection is not None:
            return self._execute_on(
                self._active_connection,
                translated_query,
                translated_parameters,
            )
        with self._pool.connection() as connection:
            return self._execute_on(connection, translated_query, translated_parameters)

    def _execute_on(
        self,
        connection: Any,
        query: str,
        parameters: tuple[object, ...],
    ) -> _BufferedPostgreSQLCursor:
        try:
            cursor = connection.execute(query, parameters)
            rows = (
                tuple(_normalize_postgresql_row(row) for row in cursor.fetchall())
                if cursor.description is not None
                else ()
            )
            return _BufferedPostgreSQLCursor(rows, int(cursor.rowcount))
        except self._integrity_error as error:
            raise sqlite3.IntegrityError("PostgreSQL catalog constraint failed") from error


class PostgreSQLCatalogRepository(SQLiteCatalogRepository):
    """Pooled, multi-instance catalog backed by the packaged PostgreSQL migrations."""

    def __init__(
        self,
        connect_kwargs: Mapping[str, object],
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        pool: Any | None = None,
    ) -> None:
        if min_pool_size < 1 or max_pool_size < min_pool_size:
            raise ValueError("Invalid PostgreSQL catalog pool bounds")
        self._lock = RLock()
        if pool is None:
            try:
                psycopg_module = import_module("psycopg")
                pool_module = import_module("psycopg_pool")
                rows_module = import_module("psycopg.rows")
                pool_constructor = cast(Any, pool_module.ConnectionPool)
                pool = pool_constructor(
                    kwargs={**dict(connect_kwargs), "row_factory": rows_module.dict_row},
                    min_size=min_pool_size,
                    max_size=max_pool_size,
                    open=True,
                    name="sqlverity-catalog",
                )
                pool.wait(timeout=30.0)
            except (AttributeError, ImportError) as error:
                raise RuntimeError(
                    "The postgres extra is required for the PostgreSQL catalog backend"
                ) from error
            integrity_error = cast(type[Exception], psycopg_module.IntegrityError)
        else:
            integrity_error = cast(
                type[Exception],
                getattr(pool, "integrity_error", sqlite3.IntegrityError),
            )
        self._pool = pool
        self._connection = cast(
            Any,
            _PostgreSQLConnectionAdapter(pool, integrity_error),
        )

    @_locked
    def initialize(self) -> None:
        migration_files = _postgresql_migration_files()
        with self._pool.connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('sqlverity_catalog_migrations'))"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sqlverity_schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            applied_rows = connection.execute(
                "SELECT version FROM sqlverity_schema_migrations"
            ).fetchall()
            applied = {str(row["version"]) for row in applied_rows}
            for migration_file in migration_files:
                if migration_file.name in applied:
                    continue
                script = _migration_body(migration_file.read_text(encoding="utf-8"))
                connection.execute(script, prepare=False)
                connection.execute(
                    "INSERT INTO sqlverity_schema_migrations (version) VALUES (%s)",
                    (migration_file.name,),
                )

    @_locked
    def close(self) -> None:
        self._pool.close()


def _translate_postgresql_query(query: str) -> str:
    translated = query.replace("?", "%s")
    translated = re.sub(
        r"ORDER BY\s+name\s+COLLATE\s+NOCASE",
        "ORDER BY lower(name)",
        translated,
        flags=re.IGNORECASE,
    )
    for repository_name, database_name in _POSTGRESQL_JSON_COLUMNS.items():
        translated = re.sub(
            rf"\b{re.escape(repository_name)}\b",
            database_name,
            translated,
        )
    return translated


def _normalize_postgresql_row(row: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        normalized_value = _normalize_postgresql_value(value)
        normalized[key] = normalized_value
        repository_alias = _POSTGRESQL_JSON_ALIASES.get(key)
        if repository_alias is not None:
            normalized[repository_alias] = normalized_value
    return normalized


def _normalize_postgresql_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return value


def _postgresql_migration_files() -> tuple[Path, ...]:
    migration_directory = Path(__file__).resolve().parents[3] / "migrations" / "postgresql"
    migration_files = tuple(sorted(migration_directory.glob("[0-9][0-9][0-9][0-9]_*.sql")))
    if not migration_files:
        raise RuntimeError("Packaged PostgreSQL catalog migrations are missing")
    return migration_files


def _migration_body(script: str) -> str:
    without_begin = re.sub(r"\A\s*BEGIN\s*;", "", script, count=1, flags=re.IGNORECASE)
    return re.sub(r"COMMIT\s*;\s*\Z", "", without_begin, count=1, flags=re.IGNORECASE)


def _require_expected_resolution(
    current: SemanticResolution | None,
    expected_updated_at: datetime | None,
) -> None:
    if current is None:
        if expected_updated_at is not None:
            raise SemanticResolutionConflictError(
                "Semantic resolution no longer matches the client"
            )
        return
    if expected_updated_at is None:
        raise SemanticResolutionConflictError(
            "expected_updated_at is required when correcting an existing resolution"
        )
    if current.updated_at != expected_updated_at:
        raise SemanticResolutionConflictError(
            "Semantic resolution changed after it was loaded"
        )


def _require_expected_concept_resolution(
    current: BusinessConceptResolution | None,
    expected_updated_at: datetime | None,
) -> None:
    if current is None:
        if expected_updated_at is not None:
            raise BusinessConceptResolutionConflictError(
                "Business concept resolution no longer matches the client"
            )
        return
    if expected_updated_at is None:
        raise BusinessConceptResolutionConflictError(
            "expected_updated_at is required when correcting an existing concept"
        )
    if current.updated_at != expected_updated_at:
        raise BusinessConceptResolutionConflictError(
            "Business concept resolution changed after it was loaded"
        )


def _require_expected_analytic_resolution(
    current: AnalyticSemanticResolution | None,
    expected_updated_at: datetime | None,
) -> None:
    if current is None:
        if expected_updated_at is not None:
            raise AnalyticSemanticResolutionConflictError(
                "Analytic semantic resolution no longer matches the client"
            )
        return
    if expected_updated_at is None:
        raise AnalyticSemanticResolutionConflictError(
            "expected_updated_at is required when correcting an existing analytic asset"
        )
    if current.updated_at != expected_updated_at:
        raise AnalyticSemanticResolutionConflictError(
            "Analytic semantic resolution changed after it was loaded"
        )


def _semantic_definition_from_row(row: sqlite3.Row) -> SemanticDefinition:
    return SemanticDefinition(
        id=row["id"],
        tenant_id=row["tenant_id"],
        catalog_version_id=row["catalog_version_id"],
        object_ref=row["object_ref"],
        description=row["description"],
        status=EpistemicStatus(row["epistemic_status"]),
        source=row["source"],
        confidence=float(row["confidence"]),
        actor_id=row["actor_id"],
        reason=row["reason"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _column_definition_from_row(row: sqlite3.Row) -> ColumnDefinition:
    return ColumnDefinition(
        id=row["id"],
        tenant_id=row["tenant_id"],
        schema_object_id=row["schema_object_id"],
        name=row["column_name"],
        physical_type=row["physical_type"],
        ordinal=int(row["ordinal"]),
        nullable=bool(row["nullable"]),
        classification=Classification(row["classification"]),
        default_expression=row["default_expression"],
        is_primary_key=bool(row["is_primary_key"]),
    )


def _semantic_resolution_from_row(row: sqlite3.Row) -> SemanticResolution:
    return SemanticResolution(
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        object_ref=row["object_ref"],
        description=row["description"],
        status=EpistemicStatus(row["epistemic_status"]),
        confidence=float(row["confidence"]),
        selected_definition_id=row["selected_definition_id"],
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _business_concept_definition_from_row(
    row: sqlite3.Row,
) -> BusinessConceptDefinition:
    return BusinessConceptDefinition(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        catalog_version_id=row["catalog_version_id"],
        concept_key=row["concept_key"],
        name=row["concept_name"],
        description=row["description"],
        synonyms=tuple(json.loads(row["synonyms_json"])),
        object_refs=tuple(json.loads(row["object_refs_json"])),
        content_classification=Classification(row["content_classification"]),
        status=EpistemicStatus(row["epistemic_status"]),
        source=row["source"],
        confidence=float(row["confidence"]),
        actor_id=row["actor_id"],
        reason=row["reason"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _business_concept_resolution_from_row(
    row: sqlite3.Row,
) -> BusinessConceptResolution:
    return BusinessConceptResolution(
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        concept_key=row["concept_key"],
        name=row["concept_name"],
        description=row["description"],
        synonyms=tuple(json.loads(row["synonyms_json"])),
        object_refs=tuple(json.loads(row["object_refs_json"])),
        content_classification=Classification(row["content_classification"]),
        status=EpistemicStatus(row["epistemic_status"]),
        confidence=float(row["confidence"]),
        selected_definition_id=row["selected_definition_id"],
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _analytic_definition_from_row(row: sqlite3.Row) -> AnalyticSemanticDefinition:
    payload = json.loads(row["payload_json"])
    common = {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "data_source_id": row["data_source_id"],
        "catalog_version_id": row["catalog_version_id"],
        "name": payload["name"],
        "description": payload["description"],
        "object_refs": tuple(payload["object_refs"]),
        "concept_keys": tuple(payload["concept_keys"]),
        "content_classification": Classification(row["content_classification"]),
        "status": EpistemicStatus(row["epistemic_status"]),
        "source": row["source"],
        "confidence": float(row["confidence"]),
        "actor_id": row["actor_id"],
        "reason": row["reason"],
        "created_at": _parse_datetime(row["created_at"]),
    }
    if AnalyticSemanticKind(row["asset_kind"]) is AnalyticSemanticKind.METRIC:
        return MetricDefinition(
            metric_key=row["asset_key"],
            expression_sql=payload["expression_sql"],
            normalized_expression_sql=payload["normalized_expression_sql"],
            grain_refs=tuple(payload["grain_refs"]),
            dimension_refs=tuple(payload["dimension_refs"]),
            rule_keys=tuple(payload["rule_keys"]),
            **common,
        )
    return BusinessRuleDefinition(
        rule_key=row["asset_key"],
        predicate_sql=payload["predicate_sql"],
        normalized_predicate_sql=payload["normalized_predicate_sql"],
        **common,
    )


def _analytic_resolution_from_row(row: sqlite3.Row) -> AnalyticSemanticResolution:
    payload = json.loads(row["payload_json"])
    common = {
        "tenant_id": row["tenant_id"],
        "data_source_id": row["data_source_id"],
        "name": payload["name"],
        "description": payload["description"],
        "object_refs": tuple(payload["object_refs"]),
        "concept_keys": tuple(payload["concept_keys"]),
        "content_classification": Classification(row["content_classification"]),
        "status": EpistemicStatus(row["epistemic_status"]),
        "confidence": float(row["confidence"]),
        "selected_definition_id": row["selected_definition_id"],
        "updated_at": _parse_datetime(row["updated_at"]),
    }
    if AnalyticSemanticKind(row["asset_kind"]) is AnalyticSemanticKind.METRIC:
        return MetricResolution(
            metric_key=row["asset_key"],
            normalized_expression_sql=payload["normalized_expression_sql"],
            grain_refs=tuple(payload["grain_refs"]),
            dimension_refs=tuple(payload["dimension_refs"]),
            rule_keys=tuple(payload["rule_keys"]),
            **common,
        )
    return BusinessRuleResolution(
        rule_key=row["asset_key"],
        normalized_predicate_sql=payload["normalized_predicate_sql"],
        **common,
    )


def _analytic_kind_and_key(
    asset: AnalyticSemanticDefinition | AnalyticSemanticResolution,
) -> tuple[AnalyticSemanticKind, str]:
    if isinstance(asset, (MetricDefinition, MetricResolution)):
        return AnalyticSemanticKind.METRIC, asset.metric_key
    return AnalyticSemanticKind.BUSINESS_RULE, asset.rule_key


def _analytic_payload(
    asset: AnalyticSemanticDefinition | AnalyticSemanticResolution,
) -> dict[str, object]:
    common: dict[str, object] = {
        "name": asset.name,
        "description": asset.description,
        "object_refs": asset.object_refs,
        "concept_keys": asset.concept_keys,
    }
    if isinstance(asset, (MetricDefinition, MetricResolution)):
        common.update(
            {
                "normalized_expression_sql": asset.normalized_expression_sql,
                "grain_refs": asset.grain_refs,
                "dimension_refs": asset.dimension_refs,
                "rule_keys": asset.rule_keys,
            }
        )
        if isinstance(asset, MetricDefinition):
            common["expression_sql"] = asset.expression_sql
    else:
        common["normalized_predicate_sql"] = asset.normalized_predicate_sql
        if isinstance(asset, BusinessRuleDefinition):
            common["predicate_sql"] = asset.predicate_sql
    return common


def _analytic_resolution_from_definition(
    definition: AnalyticSemanticDefinition,
    *,
    updated_at: datetime,
) -> AnalyticSemanticResolution:
    if isinstance(definition, MetricDefinition):
        return MetricResolution(
            tenant_id=definition.tenant_id,
            data_source_id=definition.data_source_id,
            metric_key=definition.metric_key,
            name=definition.name,
            description=definition.description,
            normalized_expression_sql=definition.normalized_expression_sql,
            object_refs=definition.object_refs,
            grain_refs=definition.grain_refs,
            dimension_refs=definition.dimension_refs,
            concept_keys=definition.concept_keys,
            rule_keys=definition.rule_keys,
            content_classification=definition.content_classification,
            status=definition.status,
            confidence=definition.confidence,
            selected_definition_id=definition.id,
            updated_at=updated_at,
        )
    return BusinessRuleResolution(
        tenant_id=definition.tenant_id,
        data_source_id=definition.data_source_id,
        rule_key=definition.rule_key,
        name=definition.name,
        description=definition.description,
        normalized_predicate_sql=definition.normalized_predicate_sql,
        object_refs=definition.object_refs,
        concept_keys=definition.concept_keys,
        content_classification=definition.content_classification,
        status=definition.status,
        confidence=definition.confidence,
        selected_definition_id=definition.id,
        updated_at=updated_at,
    )


def _llm_usage_from_row(row: sqlite3.Row) -> LLMUsageEvent:
    return LLMUsageEvent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        purpose=row["purpose"],
        estimated_input_tokens=int(row["estimated_input_tokens"]),
        estimated_output_tokens=int(row["estimated_output_tokens"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cached_input_tokens=int(row["cached_input_tokens"]),
        latency_ms=int(row["latency_ms"]),
        estimated_cost=row["estimated_cost"],
        actual_cost=row["actual_cost"],
        currency=row["currency"],
        pricing_id=row["pricing_id"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _ai_transfer_receipt_from_row(row: sqlite3.Row) -> AITransferReceipt:
    return AITransferReceipt(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        actor_id=row["actor_id"],
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        purpose=row["purpose"],
        privacy_mode=row["privacy_mode"],
        provider_policy_id=row["provider_policy_id"],
        policy_scope=row["policy_scope"],
        provider_policy_version=row["provider_policy_version"],
        declared_classification=Classification(row["declared_classification"]),
        detected_classification=Classification(row["detected_classification"]),
        effective_classification=Classification(row["effective_classification"]),
        maximum_allowed_classification=Classification(
            row["maximum_allowed_classification"]
        ),
        detection_reason_codes=tuple(json.loads(row["detection_reason_codes_json"])),
        content_counts=tuple(
            AIContentManifestCount(
                kind=item["kind"],
                classification=Classification(item["classification"]),
                included_count=int(item["included_count"]),
                redacted_count=int(item["redacted_count"]),
            )
            for item in json.loads(row["content_counts_json"])
        ),
        preflight_digest=row["preflight_digest"],
        confirmation_outcome=row["confirmation_outcome"],
        provider_invoked=bool(row["provider_invoked"]),
        decision_code=row["decision_code"],
        llm_usage_event_id=row["llm_usage_event_id"],
        query_request_id=row["query_request_id"],
        input_tokens=(int(row["input_tokens"]) if row["input_tokens"] is not None else None),
        output_tokens=(
            int(row["output_tokens"]) if row["output_tokens"] is not None else None
        ),
        latency_ms=(int(row["latency_ms"]) if row["latency_ms"] is not None else None),
        estimated_cost=row["estimated_cost"],
        actual_cost=row["actual_cost"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _security_principal_from_row(row: sqlite3.Row) -> SecurityPrincipal:
    return SecurityPrincipal(
        id=row["id"],
        tenant_id=row["tenant_id"],
        subject=row["subject"],
        display_name=row["display_name"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _api_credential_from_row(row: sqlite3.Row) -> APICredential:
    expires_at = row["expires_at"]
    return APICredential(
        id=row["id"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        label=row["label"],
        token_sha256=row["token_sha256"],
        expires_at=_parse_datetime(expires_at) if expires_at is not None else None,
        created_at=_parse_datetime(row["created_at"]),
    )


def _tenant_role_assignment_from_row(row: sqlite3.Row) -> TenantRoleAssignment:
    return TenantRoleAssignment(
        id=row["id"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        role=PlatformRole(row["role"]),
        created_by=row["created_by"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _data_source_role_assignment_from_row(
    row: sqlite3.Row,
) -> DataSourceRoleAssignment:
    return DataSourceRoleAssignment(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        principal_id=row["principal_id"],
        role=PlatformRole(row["role"]),
        created_by=row["created_by"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _api_credential_revocation_from_row(
    row: sqlite3.Row,
) -> APICredentialRevocation:
    return APICredentialRevocation(
        id=row["id"],
        tenant_id=row["tenant_id"],
        credential_id=row["credential_id"],
        actor_id=row["actor_id"],
        reason=row["reason"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _model_pricing_from_row(row: sqlite3.Row) -> ModelPricing:
    valid_to = row["valid_to"]
    cached_price = row["cached_input_price_per_unit"]
    return ModelPricing(
        id=row["id"],
        tenant_id=row["tenant_id"],
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        valid_from=_parse_datetime(row["valid_from"]),
        valid_to=_parse_datetime(valid_to) if valid_to is not None else None,
        currency=row["currency"],
        token_unit=int(row["token_unit"]),
        input_price_per_unit=Decimal(row["input_price_per_unit"]),
        cached_input_price_per_unit=(
            Decimal(cached_price) if cached_price is not None else None
        ),
        output_price_per_unit=Decimal(row["output_price_per_unit"]),
        batch_discount=Decimal(row["batch_discount"]),
        notes=row["notes"],
        source_version=row["source_version"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _tenant_budget_from_row(row: sqlite3.Row) -> TenantBudget:
    valid_to = row["valid_to"]
    return TenantBudget(
        id=row["id"],
        tenant_id=row["tenant_id"],
        currency=row["currency"],
        amount=Decimal(row["amount"]),
        period=BudgetPeriod(row["period"]),
        valid_from=_parse_datetime(row["valid_from"]),
        valid_to=_parse_datetime(valid_to) if valid_to is not None else None,
        created_at=_parse_datetime(row["created_at"]),
    )


def _execution_cost_policy_from_row(row: sqlite3.Row) -> ExecutionCostPolicy:
    return ExecutionCostPolicy(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        max_total_cost=(
            float(row["max_total_cost"])
            if row["max_total_cost"] is not None
            else None
        ),
        max_estimated_rows=(
            int(row["max_estimated_rows"])
            if row["max_estimated_rows"] is not None
            else None
        ),
        require_explain=bool(row["require_explain"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _provider_egress_policy_from_row(row: sqlite3.Row) -> ProviderEgressPolicy:
    return ProviderEgressPolicy(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        provider_id=row["provider_id"],
        allowed=bool(row["allowed"]),
        maximum_classification=Classification(row["maximum_classification"]),
        allowed_purposes=tuple(json.loads(row["allowed_purposes_json"])),
        data_residency=row["data_residency"],
        retention_mode=ProviderRetentionMode(row["retention_mode"]),
        acknowledgement_digest=row["acknowledgement_digest"],
        acknowledged_by=row["acknowledged_by"],
        acknowledged_at=(
            _parse_datetime(row["acknowledged_at"])
            if row["acknowledged_at"] is not None
            else None
        ),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _authorized_query_definition_from_row(
    row: sqlite3.Row,
) -> AuthorizedQueryDefinition:
    parameter_payload = json.loads(row["parameters_json"])
    return AuthorizedQueryDefinition(
        id=row["id"],
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        catalog_version_id=row["catalog_version_id"],
        version=int(row["definition_version"]),
        virtual_schema=row["virtual_schema"],
        virtual_name=row["virtual_name"],
        description=row["description"],
        base_sql=row["base_sql"],
        normalized_base_sql=row["normalized_base_sql"],
        parameters=tuple(
            AuthorizedQueryParameter(
                name=item["name"],
                physical_type=item["physical_type"],
                nullable=bool(item["nullable"]),
            )
            for item in parameter_payload
        ),
        allow_filtering=bool(row["allow_filtering"]),
        allow_aggregation=bool(row["allow_aggregation"]),
        created_at=_parse_datetime(row["created_at"]),
    )
