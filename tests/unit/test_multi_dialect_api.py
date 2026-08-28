from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class MultiDialectAPITests(unittest.TestCase):
    def test_mysql_ddl_import_reaches_catalog_and_schema_explorer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "MySQL tenant"}).json()
                    unsupported = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Snowflake later",
                            "source_type": "manual_schema",
                            "dialect": "snowflake",
                        },
                    )
                    self.assertEqual(422, unsupported.status_code, unsupported.text)
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "MySQL analytics",
                            "source_type": "ddl_import",
                            "dialect": "mysql",
                        },
                    )
                    self.assertEqual(201, source.status_code, source.text)
                    source_payload = source.json()
                    base_path = (
                        f"/v1/tenants/{tenant['id']}/data-sources/{source_payload['id']}"
                    )

                    missing_schema = client.post(
                        f"{base_path}/imports/ddl",
                        json={"ddl": "CREATE TABLE events (id BIGINT PRIMARY KEY)"},
                    )
                    self.assertEqual(422, missing_schema.status_code, missing_schema.text)

                    imported = client.post(
                        f"{base_path}/imports/ddl",
                        json={
                            "default_schema": "analytics",
                            "ddl": (
                                "CREATE TABLE events ("
                                "id BIGINT PRIMARY KEY, "
                                "label VARCHAR(100) COMMENT 'Event label'"
                                ") COMMENT='Events'"
                            ),
                        },
                    )
                    self.assertEqual(201, imported.status_code, imported.text)
                    self.assertEqual(1, imported.json()["object_count"])
                    self.assertEqual(2, imported.json()["column_count"])
                    self.assertEqual(2, imported.json()["imported_description_count"])

                    explored = client.get(f"{base_path}/schema")
                    self.assertEqual(200, explored.status_code, explored.text)
                    self.assertEqual("analytics.events", explored.json()["objects"][0]["reference"])

    def test_manual_mariadb_import_uses_data_source_dialect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "MariaDB tenant"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "MariaDB manual",
                            "source_type": "manual_schema",
                            "dialect": "mariadb",
                        },
                    ).json()
                    base_path = f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}"

                    imported = client.post(
                        f"{base_path}/imports/manual",
                        json={
                            "objects": [
                                {
                                    "schema_name": "analytics",
                                    "name": "customers",
                                    "kind": "table",
                                    "columns": [
                                        {
                                            "name": "id",
                                            "physical_type": "bigint",
                                            "ordinal": 1,
                                            "nullable": False,
                                            "is_primary_key": True,
                                        }
                                    ],
                                }
                            ],
                            "relationships": [],
                        },
                    )
                    self.assertEqual(201, imported.status_code, imported.text)
                    explored = client.get(f"{base_path}/schema")
                    self.assertEqual(
                        "analytics.customers",
                        explored.json()["objects"][0]["reference"],
                    )

    def test_oracle_and_sqlserver_ddl_aliases_reach_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post(
                        "/v1/tenants",
                        json={"name": "Enterprise dialects"},
                    ).json()
                    cases = (
                        (
                            "oracle",
                            "oracle",
                            "SALES",
                            "CREATE TABLE orders (id NUMBER(19) PRIMARY KEY)",
                            "SALES.orders",
                        ),
                        (
                            "mssql",
                            "sqlserver",
                            None,
                            "CREATE TABLE events (id BIGINT PRIMARY KEY)",
                            "dbo.events",
                        ),
                    )
                    for submitted, canonical, default_schema, ddl, reference in cases:
                        with self.subTest(dialect=submitted):
                            source_response = client.post(
                                f"/v1/tenants/{tenant['id']}/data-sources",
                                json={
                                    "name": f"{submitted} DDL",
                                    "source_type": "ddl_import",
                                    "dialect": submitted,
                                },
                            )
                            self.assertEqual(
                                201,
                                source_response.status_code,
                                source_response.text,
                            )
                            source = source_response.json()
                            self.assertEqual(canonical, source["dialect"])
                            base_path = (
                                f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}"
                            )
                            payload = {"ddl": ddl}
                            if default_schema is not None:
                                payload["default_schema"] = default_schema
                            imported = client.post(
                                f"{base_path}/imports/ddl",
                                json=payload,
                            )
                            self.assertEqual(201, imported.status_code, imported.text)
                            explored = client.get(f"{base_path}/schema")
                            self.assertEqual(
                                reference,
                                explored.json()["objects"][0]["reference"],
                            )


if __name__ == "__main__":
    unittest.main()
