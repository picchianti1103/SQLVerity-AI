from __future__ import annotations

import sqlite3
import unittest

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import DataSourceType, PlatformRole
from packages.security.sqlverity_security import (
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
    SecurityAccessConflictError,
    SecurityConfigurationError,
    SecurityPermission,
)

BOOTSTRAP_KEY = "test-bootstrap-api-key-with-at-least-32-chars"


class AuthenticationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        self.other_data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Finance",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        self.service = AuthenticationService(self.repository, BOOTSTRAP_KEY)

    def tearDown(self) -> None:
        self.repository.close()

    def test_bootstrap_is_environment_only_platform_authority(self) -> None:
        principal = self.service.authenticate_bearer(f"Bearer {BOOTSTRAP_KEY}")

        self.assertTrue(principal.is_bootstrap)
        self.assertEqual(principal.actor_id, "bootstrap-admin")
        self.service.authorize(principal, SecurityPermission.PLATFORM_MANAGE)
        self.service.authorize(
            principal,
            SecurityPermission.SECURITY_MANAGE,
            tenant_id=self.tenant.id,
        )
        self.assertEqual(self.repository.list_security_principals(self.tenant.id), ())

    def test_tenant_role_authenticates_and_enforces_permission_matrix(self) -> None:
        issued = self.service.provision_principal(
            tenant_id=self.tenant.id,
            subject="analyst@example.test",
            display_name="API Analyst",
            role=PlatformRole.ANALYST,
            credential_label="automation",
            created_by="bootstrap-admin",
        )

        authenticated = self.service.authenticate_bearer(f"Bearer {issued.api_key}")
        self.assertEqual(authenticated.id, issued.principal.id)
        self.assertEqual(authenticated.tenant_id, self.tenant.id)
        self.service.authorize(
            authenticated,
            SecurityPermission.QUERY_USE,
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
        )
        with self.assertRaises(AuthorizationError):
            self.service.authorize(
                authenticated,
                SecurityPermission.SEMANTIC_MANAGE,
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
            )
        with self.assertRaises(AuthorizationError):
            self.service.authorize(
                authenticated,
                SecurityPermission.READ,
                tenant_id="another-tenant",
            )

        stored = self.repository.get_api_credential(
            self.tenant.id,
            issued.credential_id,
        )
        assert stored is not None
        self.assertNotEqual(stored.token_sha256, issued.api_key)
        audit = next(
            event
            for event in self.repository.audit_events(self.tenant.id)
            if event.event_type == "security.principal_provisioned"
        )
        self.assertEqual(audit.event_type, "security.principal_provisioned")
        self.assertNotIn("token", audit.details)
        self.assertNotIn("subject", audit.details)

    def test_data_source_scope_does_not_leak_to_tenant_or_sibling_source(self) -> None:
        issued = self.service.provision_principal(
            tenant_id=self.tenant.id,
            subject="viewer@example.test",
            display_name="Scoped Viewer",
            role=PlatformRole.VIEWER,
            credential_label="viewer-key",
            created_by="bootstrap-admin",
            data_source_ids=(self.data_source.id,),
        )
        principal = self.service.authenticate_bearer(f"Bearer {issued.api_key}")

        self.service.authorize(
            principal,
            SecurityPermission.READ,
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
        )
        with self.assertRaises(AuthorizationError):
            self.service.authorize(
                principal,
                SecurityPermission.READ,
                tenant_id=self.tenant.id,
            )
        with self.assertRaises(AuthorizationError):
            self.service.authorize(
                principal,
                SecurityPermission.READ,
                tenant_id=self.tenant.id,
                data_source_id=self.other_data_source.id,
            )

    def test_revocation_is_immediate_append_only_and_content_minimizing(self) -> None:
        issued = self.service.provision_principal(
            tenant_id=self.tenant.id,
            subject="steward@example.test",
            display_name="Data Steward",
            role=PlatformRole.DATA_STEWARD,
            credential_label="steward-key",
            created_by="bootstrap-admin",
        )
        revocation = self.service.revoke_credential(
            tenant_id=self.tenant.id,
            credential_id=issued.credential_id,
            actor_id="bootstrap-admin",
            reason="Credential rotation",
        )

        with self.assertRaises(AuthenticationError):
            self.service.authenticate_bearer(f"Bearer {issued.api_key}")
        with self.assertRaises(SecurityAccessConflictError):
            self.service.revoke_credential(
                tenant_id=self.tenant.id,
                credential_id=issued.credential_id,
                actor_id="bootstrap-admin",
            )
        audit = next(
            event
            for event in self.repository.audit_events(self.tenant.id)
            if event.event_type == "security.api_credential_revoked"
        )
        self.assertEqual(audit.event_type, "security.api_credential_revoked")
        self.assertNotIn("reason", audit.details)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(  # noqa: SLF001
                "DELETE FROM api_credential_revocations WHERE id = ?",
                (revocation.id,),
            )

    def test_principal_listing_uses_bulk_access_reads(self) -> None:
        active = self.service.provision_principal(
            tenant_id=self.tenant.id,
            subject="active@example.test",
            display_name="Active analyst",
            role=PlatformRole.ANALYST,
            credential_label="active-key",
            created_by="bootstrap-admin",
        )
        revoked = self.service.provision_principal(
            tenant_id=self.tenant.id,
            subject="revoked@example.test",
            display_name="Revoked viewer",
            role=PlatformRole.VIEWER,
            credential_label="revoked-key",
            created_by="bootstrap-admin",
            data_source_ids=(self.data_source.id,),
        )
        self.service.revoke_credential(
            tenant_id=self.tenant.id,
            credential_id=revoked.credential_id,
            actor_id="bootstrap-admin",
        )

        statements: list[str] = []
        self.repository._connection.set_trace_callback(statements.append)  # noqa: SLF001
        try:
            principals = self.service.list_principals(self.tenant.id)
        finally:
            self.repository._connection.set_trace_callback(None)  # noqa: SLF001

        by_id = {entry.principal.id: entry for entry in principals}
        self.assertFalse(by_id[active.principal.id].credentials[0].revoked)
        self.assertTrue(by_id[revoked.principal.id].credentials[0].revoked)
        selects = tuple(statement.casefold() for statement in statements)
        self.assertEqual(
            1,
            sum("from api_credentials" in statement for statement in selects),
        )
        self.assertEqual(
            1,
            sum("from api_credential_revocations" in statement for statement in selects),
        )

    def test_invalid_credentials_and_duplicate_subject_fail_closed(self) -> None:
        with self.assertRaises(AuthenticationError):
            self.service.authenticate_bearer(None)
        with self.assertRaises(AuthenticationError):
            self.service.authenticate_bearer("Basic invalid")
        with self.assertRaises(AuthenticationError):
            self.service.authenticate_bearer("Bearer invalid")

        self.service.provision_principal(
            tenant_id=self.tenant.id,
            subject="duplicate@example.test",
            display_name="Duplicate",
            role=PlatformRole.ADMIN,
            credential_label="admin-key",
            created_by="bootstrap-admin",
        )
        with self.assertRaises(SecurityAccessConflictError):
            self.service.provision_principal(
                tenant_id=self.tenant.id,
                subject="duplicate@example.test",
                display_name="Duplicate",
                role=PlatformRole.ADMIN,
                credential_label="admin-key",
                created_by="bootstrap-admin",
            )

    def test_short_or_missing_bootstrap_secret_is_rejected(self) -> None:
        with self.assertRaises(SecurityConfigurationError):
            AuthenticationService(self.repository, None)
        with self.assertRaises(SecurityConfigurationError):
            AuthenticationService(self.repository, "too-short")


if __name__ == "__main__":
    unittest.main()
