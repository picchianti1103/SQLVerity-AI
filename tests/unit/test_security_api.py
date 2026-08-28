from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class SecurityAPITests(unittest.TestCase):
    def test_bearer_rbac_scopes_server_actor_and_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    missing = client.post("/v1/tenants", json={"name": "Acme"})
                    self.assertEqual(missing.status_code, 401, missing.text)
                    self.assertEqual(missing.headers["www-authenticate"], "Bearer")
                    invalid = client.post(
                        "/v1/tenants",
                        headers={"Authorization": "Bearer invalid"},
                        json={"name": "Acme"},
                    )
                    self.assertEqual(invalid.status_code, 401, invalid.text)

                    tenant = client.post(
                        "/v1/tenants",
                        headers=TEST_AUTH_HEADERS,
                        json={"name": "Acme"},
                    ).json()
                    source = self._create_source(client, tenant["id"], "Analytics")
                    sibling = self._create_source(client, tenant["id"], "Finance")
                    base_path = (
                        f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}"
                    )
                    imported = client.post(
                        f"{base_path}/imports/manual",
                        headers=TEST_AUTH_HEADERS,
                        json={
                            "objects": [
                                {
                                    "schema_name": "public",
                                    "name": "orders",
                                    "kind": "table",
                                    "columns": [
                                        {
                                            "name": "id",
                                            "physical_type": "bigint",
                                            "ordinal": 1,
                                            "nullable": False,
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                    self.assertEqual(imported.status_code, 201, imported.text)

                    steward = self._provision(
                        client,
                        tenant["id"],
                        subject="steward@example.test",
                        role="data_steward",
                    )
                    analyst = self._provision(
                        client,
                        tenant["id"],
                        subject="analyst@example.test",
                        role="analyst",
                        data_source_ids=[source["id"]],
                    )
                    viewer = self._provision(
                        client,
                        tenant["id"],
                        subject="viewer@example.test",
                        role="viewer",
                        data_source_ids=[source["id"]],
                    )
                    analyst_headers = self._headers(analyst["api_key"])
                    viewer_headers = self._headers(viewer["api_key"])
                    steward_headers = self._headers(steward["api_key"])

                    own_source = client.get(base_path, headers=analyst_headers)
                    self.assertEqual(own_source.status_code, 200, own_source.text)
                    sibling_denied = client.get(
                        (
                            f"/v1/tenants/{tenant['id']}/data-sources/"
                            f"{sibling['id']}"
                        ),
                        headers=analyst_headers,
                    )
                    self.assertEqual(sibling_denied.status_code, 403, sibling_denied.text)
                    tenant_scope_denied = client.get(
                        f"/v1/tenants/{tenant['id']}/security/principals",
                        headers=analyst_headers,
                    )
                    self.assertEqual(
                        tenant_scope_denied.status_code,
                        403,
                        tenant_scope_denied.text,
                    )

                    viewer_query_denied = client.post(
                        f"{base_path}/context/previews",
                        headers=viewer_headers,
                        json={"query": "Elenca gli ordini"},
                    )
                    self.assertEqual(
                        viewer_query_denied.status_code,
                        403,
                        viewer_query_denied.text,
                    )
                    analyst_semantic_denied = client.post(
                        f"{base_path}/semantics/corrections",
                        headers=analyst_headers,
                        json={
                            "object_ref": "public.orders",
                            "description": "Governed order records",
                        },
                    )
                    self.assertEqual(
                        analyst_semantic_denied.status_code,
                        403,
                        analyst_semantic_denied.text,
                    )
                    corrected_semantics = client.post(
                        f"{base_path}/semantics/corrections",
                        headers=steward_headers,
                        json={
                            "object_ref": "public.orders",
                            "description": "Governed order records",
                        },
                    )
                    self.assertEqual(
                        corrected_semantics.status_code,
                        201,
                        corrected_semantics.text,
                    )
                    self.assertEqual(
                        corrected_semantics.json()["definition"]["actor_id"],
                        steward["principal"]["id"],
                    )

                    spoofed = client.post(
                        f"{base_path}/learning/sql-examples",
                        headers=analyst_headers,
                        json={
                            "question": "Elenca gli ordini",
                            "corrected_sql": "SELECT id FROM public.orders",
                            "actor_id": "spoofed-administrator",
                            "content_classification": "internal",
                        },
                    )
                    self.assertEqual(spoofed.status_code, 422, spoofed.text)
                    correction = client.post(
                        f"{base_path}/learning/sql-examples",
                        headers=analyst_headers,
                        json={
                            "question": "Elenca gli ordini",
                            "corrected_sql": "SELECT id FROM public.orders",
                            "content_classification": "internal",
                        },
                    )
                    self.assertEqual(correction.status_code, 201, correction.text)
                    self.assertEqual(
                        correction.json()["example"]["actor_id"],
                        analyst["principal"]["id"],
                    )

                    principals = client.get(
                        f"/v1/tenants/{tenant['id']}/security/principals",
                        headers=TEST_AUTH_HEADERS,
                    )
                    self.assertEqual(principals.status_code, 200, principals.text)
                    self.assertEqual(len(principals.json()), 3)
                    analyst_access = next(
                        access
                        for access in principals.json()
                        if access["principal"]["id"] == analyst["principal"]["id"]
                    )
                    self.assertEqual(
                        analyst_access["credentials"][0]["id"],
                        analyst["credential_id"],
                    )
                    self.assertFalse(analyst_access["credentials"][0]["revoked"])
                    self.assertNotIn(analyst["api_key"], principals.text)
                    self.assertNotIn("token_sha256", principals.text)

                    revocation = client.post(
                        (
                            f"/v1/tenants/{tenant['id']}/security/credentials/"
                            f"{analyst['credential_id']}/revocations"
                        ),
                        headers=TEST_AUTH_HEADERS,
                        json={"reason": "Rotation"},
                    )
                    self.assertEqual(revocation.status_code, 201, revocation.text)
                    self.assertEqual(
                        revocation.json()["actor_id"],
                        "bootstrap-admin",
                    )
                    revoked = client.get(base_path, headers=analyst_headers)
                    self.assertEqual(revoked.status_code, 401, revoked.text)

                    schema = app.openapi()
                    self.assertEqual(
                        schema["components"]["securitySchemes"]["BearerAuth"]["scheme"],
                        "bearer",
                    )
                    self.assertEqual(schema["paths"]["/health"]["get"]["security"], [])

    def _create_source(
        self,
        client: TestClient,
        tenant_id: str,
        name: str,
    ) -> dict[str, Any]:
        response = client.post(
            f"/v1/tenants/{tenant_id}/data-sources",
            headers=TEST_AUTH_HEADERS,
            json={
                "name": name,
                "source_type": "manual_schema",
                "dialect": "postgresql",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return cast(dict[str, Any], response.json())

    def _provision(
        self,
        client: TestClient,
        tenant_id: str,
        *,
        subject: str,
        role: str,
        data_source_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        response = client.post(
            f"/v1/tenants/{tenant_id}/security/principals",
            headers=TEST_AUTH_HEADERS,
            json={
                "subject": subject,
                "display_name": subject.split("@", maxsplit=1)[0].title(),
                "role": role,
                "credential_label": "initial-key",
                "data_source_ids": data_source_ids or [],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return cast(dict[str, Any], response.json())

    @staticmethod
    def _headers(api_key: object) -> dict[str, str]:
        assert isinstance(api_key, str)
        return {"Authorization": f"Bearer {api_key}"}


if __name__ == "__main__":
    unittest.main()
