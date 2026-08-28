from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import PlatformRole
from packages.security.sqlverity_security import (
    AuthenticationService,
    OIDCAuthenticator,
    OIDCBrowserFlow,
    OIDCBrowserSettings,
    OIDCSettings,
)
from tests.unit.api_security import TEST_BOOTSTRAP_KEY, api_test_environment
from tests.unit.test_oidc import FakeTokenClient


class BrowserOIDCAPITests(unittest.TestCase):
    def test_cookie_session_requires_csrf_for_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            repository = SQLiteCatalogRepository(catalog_path)
            repository.initialize()
            tenant = repository.create_tenant("Federated tenant")
            AuthenticationService(repository, TEST_BOOTSTRAP_KEY).provision_federated_principal(
                tenant_id=tenant.id,
                subject="browser-user",
                display_name="Browser Admin",
                role=PlatformRole.ADMIN,
                created_by="bootstrap-admin",
            )
            repository.close()
            nonce = {"value": ""}

            def decoder(_token: str, _settings: OIDCSettings) -> Mapping[str, Any]:
                return {
                    "sub": "browser-user",
                    "sqlverity_tenant_id": tenant.id,
                    "name": "Browser Admin",
                    "amr": ["mfa"],
                    "nonce": nonce["value"],
                }

            authenticator = OIDCAuthenticator(
                OIDCSettings(
                    issuer="https://identity.example.com",
                    audience="sqlverity-api",
                    jwks_url="https://identity.example.com/jwks",
                ),
                decoder=decoder,
            )
            flow = OIDCBrowserFlow(
                OIDCBrowserSettings(
                    authorization_endpoint="https://identity.example.com/authorize",
                    token_endpoint="https://identity.example.com/token",
                    client_id="sqlverity-console",
                    redirect_uri="http://localhost/auth/oidc/callback",
                    session_secret="browser-session-secret-with-at-least-32-characters",
                    secure_cookies=False,
                ),
                authenticator,
                http_client=FakeTokenClient(),
            )
            with (
                patch.dict(os.environ, api_test_environment(catalog_path)),
                patch(
                    "apps.api.main.load_oidc_authenticator_from_environment",
                    return_value=authenticator,
                ),
                patch(
                    "apps.api.main.load_oidc_browser_flow_from_environment",
                    return_value=flow,
                ),
                TestClient(app) as client,
            ):
                login = client.get("/auth/oidc/login", follow_redirects=False)
                query = parse_qs(urlparse(login.headers["location"]).query)
                nonce["value"] = query["nonce"][0]
                callback = client.get(
                    "/auth/oidc/callback",
                    params={"code": "code", "state": query["state"][0]},
                    follow_redirects=False,
                )
                session = client.get("/auth/oidc/session")
                read = client.get(f"/v1/tenants/{tenant.id}/data-sources")
                blocked = client.post(
                    f"/v1/tenants/{tenant.id}/data-sources",
                    json={
                        "name": "Blocked",
                        "source_type": "manual_schema",
                        "dialect": "postgresql",
                    },
                )
                allowed = client.post(
                    f"/v1/tenants/{tenant.id}/data-sources",
                    headers={"X-CSRF-Token": client.cookies["sqlverity_csrf"]},
                    json={
                        "name": "Allowed",
                        "source_type": "manual_schema",
                        "dialect": "postgresql",
                    },
                )

        self.assertEqual(303, callback.status_code, callback.text)
        self.assertEqual("oidc", session.json()["authentication_method"])
        self.assertEqual(200, read.status_code, read.text)
        self.assertEqual(403, blocked.status_code, blocked.text)
        self.assertEqual(201, allowed.status_code, allowed.text)


if __name__ == "__main__":
    unittest.main()
