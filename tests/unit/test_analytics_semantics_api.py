from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class AnalyticsSemanticsAPITests(unittest.TestCase):
    def test_rule_metric_review_correction_history_and_context_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Analytics",
                            "source_type": "manual_schema",
                            "dialect": "postgresql",
                        },
                    ).json()
                    base = f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}"
                    imported = client.post(
                        f"{base}/imports/manual",
                        json={
                            "objects": [
                                {
                                    "schema_name": "public",
                                    "name": "orders",
                                    "kind": "table",
                                    "columns": [
                                        {
                                            "name": "status",
                                            "physical_type": "text",
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
                    self.assertEqual(201, imported.status_code, imported.text)
                    concept = client.post(
                        f"{base}/business-concepts/corrections",
                        json={
                            "concept_key": "gross_revenue",
                            "name": "Gross revenue",
                            "description": "Gross booked value",
                            "synonyms": ["Fatturato"],
                            "object_refs": ["public.orders.total_amount"],
                            "content_classification": "internal",
                        },
                    )
                    self.assertEqual(201, concept.status_code, concept.text)

                    rule_proposal = client.post(
                        f"{base}/business-rules/proposals",
                        json={
                            "rule_key": "valid_order",
                            "name": "Valid order",
                            "description": "Likely paid-order rule",
                            "predicate_sql": "public.orders.status = 'paid'",
                            "concept_keys": ["gross_revenue"],
                            "content_classification": "internal",
                            "status": "inferred",
                            "source": "llm_inference",
                            "confidence": 0.7,
                        },
                    )
                    self.assertEqual(201, rule_proposal.status_code, rule_proposal.text)
                    rule_updated_at = rule_proposal.json()["resolution"]["updated_at"]
                    rule = client.post(
                        f"{base}/business-rules/corrections",
                        json={
                            "rule_key": "valid_order",
                            "name": "Valid order",
                            "description": "Only paid orders are valid",
                            "predicate_sql": "public.orders.status = 'paid'",
                            "concept_keys": ["gross_revenue"],
                            "content_classification": "internal",
                            "expected_updated_at": rule_updated_at,
                        },
                    )
                    self.assertEqual(201, rule.status_code, rule.text)

                    invalid_metric = client.post(
                        f"{base}/metric-definitions/corrections",
                        json={
                            "metric_key": "gross_revenue",
                            "name": "Gross revenue",
                            "description": "Invalid non-aggregate metric",
                            "expression_sql": "public.orders.total_amount",
                            "grain_refs": ["public.orders.total_amount"],
                            "concept_keys": ["gross_revenue"],
                            "rule_keys": ["valid_order"],
                            "content_classification": "internal",
                        },
                    )
                    self.assertEqual(422, invalid_metric.status_code, invalid_metric.text)

                    metric_proposal = client.post(
                        f"{base}/metric-definitions/proposals",
                        json={
                            "metric_key": "gross_revenue",
                            "name": "Gross revenue",
                            "description": "Likely sum of paid orders",
                            "expression_sql": "SUM(public.orders.total_amount)",
                            "grain_refs": ["public.orders.total_amount"],
                            "concept_keys": ["gross_revenue"],
                            "rule_keys": ["valid_order"],
                            "content_classification": "internal",
                            "status": "inferred",
                            "source": "llm_inference",
                            "confidence": 0.8,
                        },
                    )
                    self.assertEqual(201, metric_proposal.status_code, metric_proposal.text)
                    metric_updated_at = metric_proposal.json()["resolution"]["updated_at"]

                    reviews = client.get(f"{base}/analytic-semantic-reviews")
                    self.assertEqual(200, reviews.status_code, reviews.text)
                    self.assertEqual(1, len(reviews.json()))

                    correction_payload = {
                        "metric_key": "gross_revenue",
                        "name": "Gross revenue",
                        "description": "Sum of valid gross order value",
                        "expression_sql": "SUM(public.orders.total_amount)",
                        "grain_refs": ["public.orders.total_amount"],
                        "concept_keys": ["gross_revenue"],
                        "rule_keys": ["valid_order"],
                        "content_classification": "internal",
                        "expected_updated_at": metric_updated_at,
                    }
                    metric = client.post(
                        f"{base}/metric-definitions/corrections",
                        json=correction_payload,
                    )
                    self.assertEqual(201, metric.status_code, metric.text)
                    self.assertEqual("confirmed", metric.json()["resolution"]["status"])
                    stale = client.post(
                        f"{base}/metric-definitions/corrections",
                        json=correction_payload,
                    )
                    self.assertEqual(409, stale.status_code, stale.text)

                    metrics = client.get(f"{base}/metric-definitions")
                    rules = client.get(f"{base}/business-rules")
                    self.assertEqual("gross_revenue", metrics.json()[0]["metric_key"])
                    self.assertEqual("valid_order", rules.json()[0]["rule_key"])
                    metric_history = client.get(
                        f"{base}/metric-definitions/gross_revenue/history"
                    )
                    rule_history = client.get(
                        f"{base}/business-rules/valid_order/history"
                    )
                    self.assertEqual(2, len(metric_history.json()))
                    self.assertEqual(2, len(rule_history.json()))

                    context = client.post(
                        f"{base}/context/previews",
                        json={
                            "query": "Mostra il fatturato",
                            "max_seed_objects": 1,
                            "max_objects": 1,
                            "graph_hops": 0,
                            "target_columns_per_object": 1,
                        },
                    )
                    self.assertEqual(200, context.status_code, context.text)
                    self.assertEqual("gross_revenue", context.json()["metrics"][0]["metric_key"])
                    self.assertEqual("valid_order", context.json()["business_rules"][0]["rule_key"])
                    self.assertEqual(
                        "confidential",
                        context.json()["metrics"][0]["classification"],
                    )


if __name__ == "__main__":
    unittest.main()
