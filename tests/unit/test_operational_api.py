from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class OperationalAPITests(unittest.TestCase):
    def test_readiness_metrics_request_id_and_audit_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app) as client:
                    readiness = client.get("/health/ready")
                    unauthenticated_metrics = client.get("/v1/system/metrics")
                    tenant = client.post(
                        "/v1/tenants",
                        headers=TEST_AUTH_HEADERS,
                        json={"name": "Observable tenant"},
                    )
                    metrics = client.get(
                        "/v1/system/metrics",
                        headers=TEST_AUTH_HEADERS,
                    )
                    audit = client.get(
                        f"/v1/tenants/{tenant.json()['id']}/audit/export",
                        headers=TEST_AUTH_HEADERS,
                    )

        self.assertEqual(200, readiness.status_code)
        self.assertEqual("ready", readiness.json()["status"])
        self.assertIn("X-Request-ID", readiness.headers)
        self.assertEqual(401, unauthenticated_metrics.status_code)
        self.assertEqual(200, metrics.status_code)
        self.assertIn("sqlverity_http_requests_total", metrics.text)
        self.assertEqual(200, audit.status_code)
        self.assertEqual("application/x-ndjson", audit.headers["content-type"])
        self.assertIn("tenant.created", audit.text)
        self.assertIn("attachment;", audit.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
