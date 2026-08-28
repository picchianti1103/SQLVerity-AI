from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class WebConsoleTests(unittest.TestCase):
    def test_console_shell_is_public_and_hardened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app) as client:
                    response = client.get("/ui")
                    script = client.get("/ui/assets/app.js")
                    i18n = client.get("/ui/assets/i18n.js")
                    guidance = client.get("/ui/assets/guidance.js")

        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("SQLVerity AI", response.text)
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        self.assertEqual(200, script.status_code, script.text)
        self.assertEqual(200, i18n.status_code, i18n.text)
        self.assertEqual(200, guidance.status_code, guidance.text)
        combined_scripts = script.text + i18n.text + guidance.text
        self.assertNotIn("local" + "Storage", combined_scripts)
        self.assertNotIn("session" + "Storage", combined_scripts)
        self.assertNotIn("inner" + "HTML", combined_scripts)
        self.assertIn('<html lang="en">', response.text)
        self.assertIn("globalThis.SQLVerityI18n", i18n.text)
        self.assertIn('locale: "en"', i18n.text)
        self.assertIn("globalThis.SQLVerityGuidance", guidance.text)
        self.assertIn("How SQLVerity AI understood the request", response.text)
        self.assertIn("renderInterpretation", script.text)
        self.assertIn("Correct or confirm this mapping", script.text)
        self.assertIn("saveIntentCorrection", script.text)
        self.assertIn("Correct the interpretation in your own words", response.text)
        self.assertIn("intent-corrections/from-text", script.text)
        self.assertIn("saveFreeTextIntentCorrection", script.text)
        self.assertIn("semantic memory was not changed", script.text)
        self.assertIn("Maximum privacy · one AI call", response.text)
        self.assertIn("Governed semantics · up to two AI calls", response.text)
        self.assertIn("Retry with governed semantics", response.text)
        self.assertIn("retryProposalSemantically", script.text)
        self.assertIn("force_semantic: true", script.text)
        self.assertIn("Privacy and AI Sharing", response.text)
        self.assertLess(
            response.text.index("Privacy &amp; AI"),
            response.text.index("Query Studio"),
        )
        self.assertIn("does not receive database rows", response.text.lower())
        self.assertIn("Provider/account default", response.text)
        self.assertIn("policy-acknowledged", response.text)
        self.assertIn("/sql/preflights", script.text)
        self.assertIn("confirmAITransfer", script.text)
        self.assertIn("provider not invoked", response.text)
        self.assertIn("preflight-actions", response.text)
        self.assertIn("loadPrivacyData", script.text)
        self.assertIn("Sign in with SSO", response.text)
        self.assertIn("Administer access and operations", response.text)
        self.assertIn("security/federated-principals", script.text)
        self.assertIn("provider-egress-policies", script.text)
        self.assertIn("semantics/inference-jobs", script.text)
        self.assertIn("sqlverity_csrf", script.text)
        self.assertIn('id="onboarding-list"', response.text)
        self.assertIn('id="help-drawer"', response.text)
        self.assertIn("renderOnboarding", script.text)
        self.assertIn("Control Privacy and AI Sharing", guidance.text)
        self.assertEqual(6, response.text.count('class="page-summary"'))
        self.assertIn("Access &amp; workspace", response.text)
        self.assertIn('id="source-mode-help"', response.text)
        self.assertIn("Use recommended permissions", response.text)
        self.assertIn("updateAcquisitionOptions", script.text)
        self.assertIn("Advanced PostgreSQL virtual query surface", script.text)
        self.assertIn("Local sharing check", response.text)
        self.assertIn("1 · Check what AI would receive", response.text)
        self.assertIn("5 · Execute read-only", response.text)
        self.assertIn('id="inference-provider"', response.text)
        self.assertIn("federatedRoleGuidance", script.text)
        for legacy_copy in (
            "Come ho capito la richiesta",
            "Privacy e condivisione con l’AI",
            "Accedi con SSO",
            "Amministra accessi e policy",
        ):
            self.assertNotIn(legacy_copy, response.text + combined_scripts)

    def test_capabilities_and_discovery_require_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app) as client:
                    unauthenticated = client.get("/v1/system/capabilities")
                    capabilities = client.get(
                        "/v1/system/capabilities",
                        headers=TEST_AUTH_HEADERS,
                    )

        self.assertEqual(401, unauthenticated.status_code)
        self.assertEqual(200, capabilities.status_code, capabilities.text)
        payload = capabilities.json()
        self.assertEqual("sqlite", payload["catalog_backend"])
        self.assertEqual(
            ["mariadb", "mysql", "oracle", "postgresql", "sqlserver"],
            payload["supported_dialects"],
        )
        self.assertEqual([], payload["configured_provider_ids"])

    def test_capabilities_expose_all_configured_cloud_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            configured = {
                "kimi": object(),
                "anthropic": object(),
                "gemini": object(),
            }
            with (
                patch.dict(os.environ, api_test_environment(catalog_path)),
                patch(
                    "apps.api.main.load_llm_providers_from_environment",
                    return_value=configured,
                ),
            ):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    capabilities = client.get("/v1/system/capabilities")

        self.assertEqual(200, capabilities.status_code, capabilities.text)
        self.assertEqual(
            ["anthropic", "gemini", "kimi"],
            capabilities.json()["configured_provider_ids"],
        )

    def test_console_discovery_lists_tenants_and_scoped_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    first_tenant = client.post("/v1/tenants", json={"name": "Acme"})
                    second_tenant = client.post("/v1/tenants", json={"name": "Other"})
                    tenant_id = first_tenant.json()["id"]
                    source = client.post(
                        f"/v1/tenants/{tenant_id}/data-sources",
                        json={
                            "name": "Oracle ERP",
                            "source_type": "direct_db",
                            "dialect": "oracle",
                            "capabilities": ["introspect", "explain"],
                            "connection_secret_ref": "ERP_DB",
                        },
                    )
                    tenants = client.get("/v1/tenants")
                    sources = client.get(f"/v1/tenants/{tenant_id}/data-sources")
                    other_sources = client.get(
                        f"/v1/tenants/{second_tenant.json()['id']}/data-sources"
                    )

        self.assertEqual(201, first_tenant.status_code, first_tenant.text)
        self.assertEqual(201, second_tenant.status_code, second_tenant.text)
        self.assertEqual(201, source.status_code, source.text)
        self.assertEqual(["Acme", "Other"], [item["name"] for item in tenants.json()])
        self.assertEqual([source.json()["id"]], [item["id"] for item in sources.json()])
        self.assertEqual([], other_sources.json())
