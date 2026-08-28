from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from packages.catalog.sqlverity_catalog.ingestion import DataSourceNotFoundError
from packages.catalog.sqlverity_catalog.repository import (
    SecurityConflictError,
    SQLiteCatalogRepository,
)
from packages.domain.sqlverity_domain.models import (
    APICredential,
    APICredentialRevocation,
    DataSourceRoleAssignment,
    PlatformRole,
    SecurityPrincipal,
    TenantRoleAssignment,
    utc_now,
)

from .oidc import OIDCAuthenticationError, OIDCAuthenticator


class SecurityConfigurationError(RuntimeError):
    pass


class AuthenticationError(PermissionError):
    pass


class AuthorizationError(PermissionError):
    pass


class SecurityAccessConflictError(RuntimeError):
    pass


class CredentialNotFoundError(LookupError):
    pass


class SecurityPermission(StrEnum):
    PLATFORM_MANAGE = "platform.manage"
    SECURITY_MANAGE = "security.manage"
    DATA_SOURCE_MANAGE = "data_source.manage"
    SEMANTIC_MANAGE = "semantic.manage"
    QUERY_USE = "query.use"
    QUERY_APPROVE = "query.approve"
    FEEDBACK_WRITE = "feedback.write"
    GOLDEN_REVIEW = "golden.review"
    FINOPS_MANAGE = "finops.manage"
    AUDIT_READ = "audit.read"
    READ = "read"


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    id: str
    subject: str
    display_name: str
    tenant_id: str | None
    credential_id: str | None
    is_bootstrap: bool = False
    authentication_method: str = "api_key"
    mfa_verified: bool = False

    @property
    def actor_id(self) -> str:
        return self.id


@dataclass(frozen=True, slots=True)
class IssuedAccess:
    principal: SecurityPrincipal
    credential_id: str
    api_key: str
    role: PlatformRole
    data_source_ids: tuple[str, ...]
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class PrincipalAccess:
    principal: SecurityPrincipal
    tenant_roles: tuple[PlatformRole, ...]
    data_source_roles: tuple[DataSourceRoleAssignment, ...]
    credentials: tuple[CredentialMetadata, ...]


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    id: str
    label: str
    expires_at: datetime | None
    created_at: datetime
    revoked: bool


class AuthenticationService:
    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        bootstrap_api_key: str | None,
        oidc_authenticator: OIDCAuthenticator | None = None,
    ) -> None:
        if bootstrap_api_key is None or len(bootstrap_api_key) < 32:
            raise SecurityConfigurationError(
                "SQLVERITY_BOOTSTRAP_API_KEY must contain at least 32 characters"
            )
        self._repository = repository
        self._bootstrap_hash = _token_hash(bootstrap_api_key)
        self._oidc_authenticator = oidc_authenticator

    def authenticate_bearer(self, authorization: str | None) -> AuthenticatedPrincipal:
        token = self._parse_bearer(authorization)
        token_sha256 = _token_hash(token)
        if hmac.compare_digest(token_sha256, self._bootstrap_hash):
            return AuthenticatedPrincipal(
                id="bootstrap-admin",
                subject="bootstrap-admin",
                display_name="Bootstrap administrator",
                tenant_id=None,
                credential_id=None,
                is_bootstrap=True,
            )
        credential = self._repository.get_api_credential_by_hash(token_sha256)
        if credential is None:
            return self._authenticate_oidc(token)
        if credential.expires_at is not None and credential.expires_at <= utc_now():
            raise AuthenticationError("Invalid or expired bearer credential")
        if (
            self._repository.get_api_credential_revocation(
                credential.tenant_id,
                credential.id,
            )
            is not None
        ):
            raise AuthenticationError("Invalid or expired bearer credential")
        principal = self._repository.get_security_principal(
            credential.tenant_id,
            credential.principal_id,
        )
        if principal is None:
            raise AuthenticationError("Invalid or expired bearer credential")
        return AuthenticatedPrincipal(
            id=principal.id,
            subject=principal.subject,
            display_name=principal.display_name,
            tenant_id=principal.tenant_id,
            credential_id=credential.id,
        )

    def _authenticate_oidc(self, token: str) -> AuthenticatedPrincipal:
        if self._oidc_authenticator is None:
            raise AuthenticationError("Invalid or expired bearer credential")
        try:
            identity = self._oidc_authenticator.authenticate(token)
        except OIDCAuthenticationError as error:
            raise AuthenticationError("Invalid or expired bearer credential") from error
        principal = self._repository.get_security_principal_by_subject(
            identity.tenant_id,
            identity.subject,
        )
        if principal is None:
            raise AuthenticationError("Federated principal is not provisioned")
        return AuthenticatedPrincipal(
            id=principal.id,
            subject=principal.subject,
            display_name=principal.display_name,
            tenant_id=principal.tenant_id,
            credential_id=None,
            authentication_method="oidc",
            mfa_verified=identity.mfa_verified,
        )

    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        permission: SecurityPermission,
        *,
        tenant_id: str | None = None,
        data_source_id: str | None = None,
    ) -> None:
        if principal.is_bootstrap:
            return
        if permission is SecurityPermission.PLATFORM_MANAGE:
            raise AuthorizationError("Platform administration requires bootstrap authority")
        if tenant_id is None or principal.tenant_id != tenant_id:
            raise AuthorizationError("Principal is outside this tenant")
        tenant_roles = {
            assignment.role
            for assignment in self._repository.list_tenant_role_assignments(
                tenant_id,
                principal.id,
            )
        }
        roles = set(tenant_roles)
        if data_source_id is not None:
            roles.update(
                assignment.role
                for assignment in self._repository.list_data_source_role_assignments(
                    tenant_id,
                    principal_id=principal.id,
                    data_source_id=data_source_id,
                )
            )
        if not roles & _PERMISSION_ROLES[permission]:
            scope = (
                f"DataSource {data_source_id}"
                if data_source_id is not None
                else f"tenant {tenant_id}"
            )
            raise AuthorizationError(
                f"Principal lacks {permission.value} permission for {scope}"
            )

    def provision_principal(
        self,
        *,
        tenant_id: str,
        subject: str,
        display_name: str,
        role: PlatformRole,
        credential_label: str,
        created_by: str,
        data_source_ids: tuple[str, ...] = (),
        expires_at: datetime | None = None,
    ) -> IssuedAccess:
        if self._repository.get_tenant(tenant_id) is None:
            raise LookupError("Tenant does not exist")
        if len(data_source_ids) != len(set(data_source_ids)):
            raise ValueError("DataSource security scopes must be unique")
        for data_source_id in data_source_ids:
            if self._repository.get_data_source(tenant_id, data_source_id) is None:
                raise DataSourceNotFoundError(
                    "Security scope contains a missing tenant DataSource"
                )
        created_at = utc_now()
        principal = SecurityPrincipal(
            tenant_id=tenant_id,
            subject=subject.strip(),
            display_name=display_name.strip(),
            created_at=created_at,
        )
        api_key = f"sqlverity_{secrets.token_urlsafe(32)}"
        credential = APICredential(
            tenant_id=tenant_id,
            principal_id=principal.id,
            label=credential_label.strip(),
            token_sha256=_token_hash(api_key),
            expires_at=expires_at,
            created_at=created_at,
        )
        tenant_assignment = (
            TenantRoleAssignment(
                tenant_id=tenant_id,
                principal_id=principal.id,
                role=role,
                created_by=created_by,
                created_at=created_at,
            )
            if not data_source_ids
            else None
        )
        data_source_assignments = tuple(
            DataSourceRoleAssignment(
                tenant_id=tenant_id,
                data_source_id=data_source_id,
                principal_id=principal.id,
                role=role,
                created_by=created_by,
                created_at=created_at,
            )
            for data_source_id in data_source_ids
        )
        try:
            self._repository.create_security_access(
                principal,
                credential,
                tenant_assignment=tenant_assignment,
                data_source_assignments=data_source_assignments,
            )
        except SecurityConflictError as error:
            raise SecurityAccessConflictError(str(error)) from error
        return IssuedAccess(
            principal=principal,
            credential_id=credential.id,
            api_key=api_key,
            role=role,
            data_source_ids=data_source_ids,
            expires_at=expires_at,
        )

    def list_principals(
        self,
        tenant_id: str,
    ) -> tuple[PrincipalAccess, ...]:
        tenant_roles: defaultdict[str, list[PlatformRole]] = defaultdict(list)
        for tenant_assignment in self._repository.list_tenant_role_assignments(tenant_id):
            tenant_roles[tenant_assignment.principal_id].append(tenant_assignment.role)

        data_source_roles: defaultdict[
            str, list[DataSourceRoleAssignment]
        ] = defaultdict(list)
        for data_source_assignment in self._repository.list_data_source_role_assignments(
            tenant_id
        ):
            data_source_roles[data_source_assignment.principal_id].append(
                data_source_assignment
            )

        credentials: defaultdict[str, list[APICredential]] = defaultdict(list)
        for credential in self._repository.list_api_credentials(tenant_id):
            credentials[credential.principal_id].append(credential)
        revoked_ids = frozenset(
            revocation.credential_id
            for revocation in self._repository.list_api_credential_revocations(tenant_id)
        )

        return tuple(
            PrincipalAccess(
                principal=principal,
                tenant_roles=tuple(tenant_roles[principal.id]),
                data_source_roles=tuple(data_source_roles[principal.id]),
                credentials=tuple(
                    CredentialMetadata(
                        id=credential.id,
                        label=credential.label,
                        expires_at=credential.expires_at,
                        created_at=credential.created_at,
                        revoked=credential.id in revoked_ids,
                    )
                    for credential in credentials[principal.id]
                ),
            )
            for principal in self._repository.list_security_principals(tenant_id)
        )

    def provision_federated_principal(
        self,
        *,
        tenant_id: str,
        subject: str,
        display_name: str,
        role: PlatformRole,
        created_by: str,
        data_source_ids: tuple[str, ...] = (),
    ) -> PrincipalAccess:
        if self._repository.get_tenant(tenant_id) is None:
            raise LookupError("Tenant does not exist")
        if len(data_source_ids) != len(set(data_source_ids)):
            raise ValueError("DataSource security scopes must be unique")
        for data_source_id in data_source_ids:
            if self._repository.get_data_source(tenant_id, data_source_id) is None:
                raise DataSourceNotFoundError(
                    "Security scope contains a missing tenant DataSource"
                )
        created_at = utc_now()
        principal = SecurityPrincipal(
            tenant_id=tenant_id,
            subject=subject.strip(),
            display_name=display_name.strip(),
            created_at=created_at,
        )
        tenant_assignment = (
            TenantRoleAssignment(
                tenant_id=tenant_id,
                principal_id=principal.id,
                role=role,
                created_by=created_by,
                created_at=created_at,
            )
            if not data_source_ids
            else None
        )
        data_source_assignments = tuple(
            DataSourceRoleAssignment(
                tenant_id=tenant_id,
                data_source_id=data_source_id,
                principal_id=principal.id,
                role=role,
                created_by=created_by,
                created_at=created_at,
            )
            for data_source_id in data_source_ids
        )
        try:
            self._repository.create_federated_security_access(
                principal,
                tenant_assignment=tenant_assignment,
                data_source_assignments=data_source_assignments,
            )
        except SecurityConflictError as error:
            raise SecurityAccessConflictError(str(error)) from error
        return PrincipalAccess(
            principal=principal,
            tenant_roles=(role,) if tenant_assignment is not None else (),
            data_source_roles=data_source_assignments,
            credentials=(),
        )

    def revoke_credential(
        self,
        *,
        tenant_id: str,
        credential_id: str,
        actor_id: str,
        reason: str | None = None,
    ) -> APICredentialRevocation:
        if self._repository.get_api_credential(tenant_id, credential_id) is None:
            raise CredentialNotFoundError("API credential does not exist in this tenant")
        revocation = APICredentialRevocation(
            tenant_id=tenant_id,
            credential_id=credential_id,
            actor_id=actor_id,
            reason=reason.strip() if reason is not None else None,
        )
        try:
            return self._repository.create_api_credential_revocation(revocation)
        except SecurityConflictError as error:
            raise SecurityAccessConflictError(str(error)) from error

    @staticmethod
    def _parse_bearer(authorization: str | None) -> str:
        if authorization is None:
            raise AuthenticationError("Bearer credential is required")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.casefold() != "bearer" or not token.strip():
            raise AuthenticationError("Bearer credential is required")
        return token.strip()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_PERMISSION_ROLES: dict[SecurityPermission, frozenset[PlatformRole]] = {
    SecurityPermission.PLATFORM_MANAGE: frozenset(),
    SecurityPermission.SECURITY_MANAGE: frozenset({PlatformRole.ADMIN}),
    SecurityPermission.DATA_SOURCE_MANAGE: frozenset(
        {PlatformRole.ADMIN, PlatformRole.DATA_STEWARD}
    ),
    SecurityPermission.SEMANTIC_MANAGE: frozenset(
        {PlatformRole.ADMIN, PlatformRole.DATA_STEWARD}
    ),
    SecurityPermission.QUERY_USE: frozenset(
        {PlatformRole.ADMIN, PlatformRole.DATA_STEWARD, PlatformRole.ANALYST}
    ),
    SecurityPermission.QUERY_APPROVE: frozenset(
        {PlatformRole.ADMIN, PlatformRole.DATA_STEWARD, PlatformRole.ANALYST}
    ),
    SecurityPermission.FEEDBACK_WRITE: frozenset(
        {PlatformRole.ADMIN, PlatformRole.DATA_STEWARD, PlatformRole.ANALYST}
    ),
    SecurityPermission.GOLDEN_REVIEW: frozenset(
        {PlatformRole.ADMIN, PlatformRole.DATA_STEWARD}
    ),
    SecurityPermission.FINOPS_MANAGE: frozenset({PlatformRole.ADMIN}),
    SecurityPermission.AUDIT_READ: frozenset(
        {PlatformRole.ADMIN, PlatformRole.DATA_STEWARD}
    ),
    SecurityPermission.READ: frozenset(PlatformRole),
}
