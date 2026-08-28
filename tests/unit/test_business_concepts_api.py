from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class BusinessConceptAPITests(unittest.TestCase):
    def test_proposal_review_correction_resolution_and_context_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Business catalog",
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
                                            "name": "total_amount",
                                            "physical_type": "numeric(18,2)",
                                            "ordinal": 1,
                                            "nullable": False,
                                            "classification": "confidential",
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                    self.assertEqual(201, imported.status_code, imported.text)
                    proposal_path = f"{base_path}/business-concepts/proposals"
                    proposal = {
                        "concept_key": "gross_revenue",
                        "name": "Gross revenue",
                        "description": "Likely gross booked order value",
                        "synonyms": ["Fatturato lordo"],
                        "object_refs": ["public.orders.total_amount"],
                        "content_classification": "internal",
                        "status": "inferred",
                        "source": "llm_inference",
                        "confidence": 0.72,
                    }

                    first = client.post(proposal_path, json=proposal)
                    self.assertEqual(201, first.status_code, first.text)
                    self.assertEqual("accept", first.json()["action"])

                    second = client.post(
                        proposal_path,
                        json={
                            **proposal,
                            "description": "Likely net paid order value",
                        },
                    )
                    self.assertEqual(201, second.status_code, second.text)
                    self.assertEqual("conflicting", second.json()["resolution"]["status"])
                    conflict_updated_at = second.json()["resolution"]["updated_at"]

                    reviews = client.get(f"{base_path}/business-concept-reviews")
                    self.assertEqual(200, reviews.status_code, reviews.text)
                    self.assertEqual(2, len(reviews.json()[0]["evidence"]))

                    correction_payload = {
                        "concept_key": "gross_revenue",
                        "name": "Gross revenue",
                        "description": "Gross booked order value before refunds",
                        "synonyms": ["Fatturato lordo", "Ricavi lordi"],
                        "object_refs": ["public.orders.total_amount"],
                        "content_classification": "internal",
                        "reason": "Approved by Finance",
                        "expected_updated_at": conflict_updated_at,
                    }
                    corrected = client.post(
                        f"{base_path}/business-concepts/corrections",
                        json=correction_payload,
                    )
                    self.assertEqual(201, corrected.status_code, corrected.text)
                    self.assertEqual("confirmed", corrected.json()["resolution"]["status"])

                    stale = client.post(
                        f"{base_path}/business-concepts/corrections",
                        json=correction_payload,
                    )
                    self.assertEqual(409, stale.status_code, stale.text)

                    concepts = client.get(f"{base_path}/business-concepts")
                    self.assertEqual(200, concepts.status_code, concepts.text)
                    self.assertEqual("gross_revenue", concepts.json()[0]["concept_key"])

                    history = client.get(
                        f"{base_path}/business-concepts/gross_revenue/history"
                    )
                    self.assertEqual(200, history.status_code, history.text)
                    self.assertEqual(3, len(history.json()))
                    self.assertTrue(history.json()[0]["selected"])

                    resolution = client.post(
                        f"{base_path}/business-terms/resolution",
                        json={"query": "Mostra il FATTURATO LÓRDO"},
                    )
                    self.assertEqual(200, resolution.status_code, resolution.text)
                    self.assertEqual(
                        "gross_revenue",
                        resolution.json()["matches"][0]["resolution"]["concept_key"],
                    )

                    context = client.post(
                        f"{base_path}/context/previews",
                        json={
                            "query": "Mostra il fatturato lordo",
                            "max_seed_objects": 1,
                            "max_objects": 1,
                            "graph_hops": 0,
                            "target_columns_per_object": 1,
                        },
                    )
                    self.assertEqual(200, context.status_code, context.text)
                    self.assertEqual("public.orders", context.json()["objects"][0]["reference"])
                    self.assertEqual(
                        "confidential",
                        context.json()["business_concepts"][0]["classification"],
                    )

                    collision = client.post(
                        f"{base_path}/business-concepts/corrections",
                        json={
                            **correction_payload,
                            "concept_key": "net_revenue",
                            "name": "Net revenue",
                            "expected_updated_at": None,
                        },
                    )
                    self.assertEqual(409, collision.status_code, collision.text)


if __name__ == "__main__":
    unittest.main()
