from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import DataSourceType, PlatformRole
from packages.security.sqlverity_security import AuthenticationService
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class SessionDiscoveryTests(unittest.TestCase):
    def test_discovery_is_filtered_without_expanding_scoped_roles(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                api_test_environment(Path(directory) / "catalog.sqlite3"),
            ),
            TestClient(app) as client,
        ):
            catalog = cast(SQLiteCatalogRepository, app.state.catalog)
            security = cast(AuthenticationService, app.state.security)
            tenant = catalog.create_tenant("Allowed tenant")
            other = catalog.create_tenant("Hidden tenant")
            sources = [
                catalog.create_data_source(
                    tenant_id=tenant.id,
                    name=name,
                    source_type=DataSourceType.MANUAL_SCHEMA,
                    dialect="postgresql",
                    connection_secret_ref="env://PRIVATE_REFERENCE",
                )
                for name in ("Allowed source", "Hidden sibling")
            ]
            self.assertEqual(401, client.get("/v1/session").status_code)
            for role in (PlatformRole.ANALYST, PlatformRole.VIEWER):
                access = security.provision_principal(
                    tenant_id=tenant.id,
                    subject=role.value,
                    display_name=role.value,
                    role=role,
                    credential_label="test",
                    created_by="bootstrap-admin",
                    data_source_ids=(sources[0].id,),
                )
                headers = {"Authorization": f"Bearer {access.api_key}"}
                response = client.get("/v1/session", headers=headers)
                self.assertEqual(200, response.status_code, response.text)
                payload = response.json()
                self.assertEqual([], payload["platform_permissions"])
                self.assertEqual(access.principal.id, payload["principal"]["id"])
                self.assertEqual([tenant.id], [item["id"] for item in payload["tenants"]])
                discovered = payload["tenants"][0]
                self.assertEqual([], discovered["permissions"])
                self.assertEqual(
                    [sources[0].id], [item["source"]["id"] for item in discovered["data_sources"]]
                )
                self.assertEqual(
                    role is PlatformRole.ANALYST,
                    "query.use" in discovered["data_sources"][0]["permissions"],
                )
                for hidden in (other.id, sources[1].id, "PRIVATE_REFERENCE", access.api_key):
                    self.assertNotIn(hidden, response.text)
                base = f"/v1/tenants/{tenant.id}/data-sources"
                self.assertEqual(
                    200, client.get(f"{base}/{sources[0].id}", headers=headers).status_code
                )
                self.assertEqual(
                    403, client.get(f"{base}/{sources[1].id}", headers=headers).status_code
                )
                self.assertEqual(403, client.get(base, headers=headers).status_code)
                self.assertEqual(
                    403,
                    client.get(
                        f"/v1/tenants/{tenant.id}/security/principals",
                        headers=headers,
                    ).status_code,
                )
                self.assertEqual(
                    (),
                    catalog.list_tenant_role_assignments(
                        tenant.id,
                        access.principal.id,
                    ),
                )
                security.revoke_credential(
                    tenant_id=tenant.id,
                    credential_id=access.credential_id,
                    actor_id="bootstrap-admin",
                )
                self.assertEqual(401, client.get("/v1/session", headers=headers).status_code)
            bootstrap = client.get("/v1/session", headers=TEST_AUTH_HEADERS).json()
            self.assertEqual({tenant.id, other.id}, {item["id"] for item in bootstrap["tenants"]})
