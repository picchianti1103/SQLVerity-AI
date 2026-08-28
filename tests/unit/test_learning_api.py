from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class LearningLoopAPITests(unittest.TestCase):
    def test_correction_retrieval_supersession_and_context_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Learning catalog",
                            "source_type": "manual_schema",
                            "dialect": "postgresql",
                        },
                    ).json()
                    base_path = (
                        f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}"
                    )
                    imported = client.post(
                        f"{base_path}/imports/manual",
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
                                        },
                                        {
                                            "name": "total_amount",
                                            "physical_type": "numeric(18,2)",
                                            "ordinal": 2,
                                            "nullable": False,
                                            "classification": "confidential",
                                        },
                                    ],
                                }
                            ]
                        },
                    )
                    self.assertEqual(imported.status_code, 201, imported.text)
                    examples_path = f"{base_path}/learning/sql-examples"
                    payload = {
                        "question": "Qual è il fatturato?",
                        "corrected_sql": "SELECT total_amount FROM public.orders",
                        "content_classification": "internal",
                        "business_concepts": ["gross_revenue"],
                        "reason": "Definition approved by Finance",
                    }

                    first = client.post(examples_path, json=payload)

                    self.assertEqual(first.status_code, 201, first.text)
                    first_body = first.json()
                    self.assertTrue(first_body["is_active"])
                    self.assertEqual(first_body["example"]["revision"], 1)
                    first_id = first_body["example"]["id"]

                    duplicate = client.post(examples_path, json=payload)
                    self.assertEqual(duplicate.status_code, 409, duplicate.text)

                    second = client.post(
                        examples_path,
                        json={
                            **payload,
                            "corrected_sql": (
                                "SELECT total_amount FROM public.orders "
                                "WHERE total_amount > 0"
                            ),
                            "supersedes_example_id": first_id,
                        },
                    )
                    self.assertEqual(second.status_code, 201, second.text)
                    self.assertEqual(second.json()["example"]["revision"], 2)

                    history = client.get(
                        examples_path,
                        params={"include_superseded": True},
                    )
                    self.assertEqual(history.status_code, 200, history.text)
                    self.assertEqual(
                        [entry["is_active"] for entry in history.json()],
                        [False, True],
                    )

                    retrieved = client.post(
                        f"{examples_path}/retrieval",
                        json={"question": "Dimmi il fatturato", "max_results": 3},
                    )
                    self.assertEqual(retrieved.status_code, 200, retrieved.text)
                    self.assertEqual(len(retrieved.json()), 1)
                    self.assertGreater(retrieved.json()[0]["score"], 0.2)

                    context = client.post(
                        f"{base_path}/context/previews",
                        json={
                            "query": "Dimmi il fatturato",
                            "max_seed_objects": 1,
                            "max_objects": 1,
                            "graph_hops": 0,
                            "target_columns_per_object": 1,
                        },
                    )
                    self.assertEqual(context.status_code, 200, context.text)
                    self.assertEqual(
                        context.json()["objects"][0]["reference"],
                        "public.orders",
                    )
                    self.assertEqual(
                        context.json()["sql_examples"][0]["classification"],
                        "confidential",
                    )

                    unsafe = client.post(
                        examples_path,
                        json={
                            **payload,
                            "question": "Cancella ordini",
                            "corrected_sql": "DELETE FROM public.orders",
                        },
                    )
                    self.assertEqual(unsafe.status_code, 422, unsafe.text)


if __name__ == "__main__":
    unittest.main()
