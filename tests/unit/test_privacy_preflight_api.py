from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.domain.sqlverity_domain.contracts import LLMResponse, TokenEstimate
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class APIPreflightProviderSpy:
    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            payload={
                "intent": "data_query",
                "interpretation": {
                    "kind": "record_list",
                    "summary": "Elenca gli ordini.",
                    "requested_row_limit": None,
                    "entities": [
                        {
                            "term": "orders",
                            "object_ref": "public.orders",
                            "role": "primary_table",
                            "confidence": 1.0,
                            "reason": "Tabella richiesta.",
                            "alternatives": [],
                        }
                    ],
                },
                "sql": "SELECT id FROM public.orders",
                "dialect": "postgresql",
                "tables": ["public.orders"],
                "columns": ["public.orders.id"],
                "business_concepts": [],
                "metrics": [],
                "business_rules": [],
                "assumptions": [],
                "parameters": [],
                "ambiguities": [],
                "needs_clarification": False,
            },
            model_id="api-privacy-model",
            input_tokens=90,
            output_tokens=20,
            latency_ms=12,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(input_tokens=100, output_tokens=30)

    def capabilities(self) -> Mapping[str, Any]:
        return {"structured_output": True, "model_id": "api-privacy-model"}

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}


class PrivacyPreflightAPITests(unittest.TestCase):
    def test_preflight_confirmation_effective_policy_and_receipt(self) -> None:
        provider = APIPreflightProviderSpy()
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            environment = api_test_environment(catalog_path)
            with (
                patch.dict(os.environ, environment),
                patch(
                    "apps.api.main.load_llm_providers_from_environment",
                    return_value={"fake": provider},
                ),
                TestClient(app, headers=TEST_AUTH_HEADERS) as client,
            ):
                tenant = client.post("/v1/tenants", json={"name": "Privacy API"})
                tenant_id = tenant.json()["id"]
                source = client.post(
                    f"/v1/tenants/{tenant_id}/data-sources",
                    json={
                        "name": "Orders",
                        "source_type": "manual_schema",
                        "dialect": "postgresql",
                        "capabilities": [],
                    },
                )
                source_id = source.json()["id"]
                base = f"/v1/tenants/{tenant_id}/data-sources/{source_id}"
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
                                        "name": "id",
                                        "physical_type": "bigint",
                                        "ordinal": 1,
                                        "nullable": False,
                                        "classification": "internal",
                                    }
                                ],
                            }
                        ],
                        "relationships": [],
                    },
                )
                self.assertEqual(201, imported.status_code, imported.text)
                policy = client.put(
                    f"{base}/provider-egress-policies/fake",
                    json={
                        "allowed": True,
                        "maximum_classification": "internal",
                        "allowed_purposes": ["sql_proposal_generation"],
                        "data_residency": "unspecified",
                        "retention_mode": "provider_default",
                        "acknowledged": True,
                    },
                )
                self.assertEqual(200, policy.status_code, policy.text)
                self.assertFalse(policy.json()["review_required"])

                providers = client.get(f"{base}/privacy/providers")
                self.assertEqual(200, providers.status_code, providers.text)
                self.assertEqual("api-privacy-model", providers.json()[0]["deployment"]["model_id"])
                self.assertFalse(providers.json()[0]["review_required"])
                self.assertTrue(providers.json()[0]["deployment_matches_policy"])
                self.assertEqual("allowed", providers.json()[0]["decision_code"])

                preflight = client.post(
                    f"{base}/sql/preflights",
                    json={
                        "provider_id": "fake",
                        "query": "Show orders",
                        "question_classification": "internal",
                        "max_seed_objects": 1,
                        "max_objects": 1,
                        "graph_hops": 0,
                    },
                )
                self.assertEqual(201, preflight.status_code, preflight.text)
                disclosure = preflight.json()
                self.assertTrue(disclosure["allowed"])
                self.assertFalse(disclosure["provider_invoked"])
                self.assertIsNotNone(disclosure["confirmation_token"])
                self.assertEqual(0, provider.calls)

                generated = client.post(
                    f"{base}/sql/proposals",
                    json={
                        "provider_id": "fake",
                        "query": "Show orders",
                        "question_classification": "internal",
                        "max_seed_objects": 1,
                        "max_objects": 1,
                        "graph_hops": 0,
                        "confirmation_token": disclosure["confirmation_token"],
                    },
                )
                self.assertEqual(201, generated.status_code, generated.text)
                self.assertEqual(1, provider.calls)
                self.assertTrue(generated.json()["transfer_receipt"]["provider_invoked"])

                replay = client.post(
                    f"{base}/sql/proposals",
                    json={
                        "provider_id": "fake",
                        "query": "Show orders",
                        "question_classification": "internal",
                        "max_seed_objects": 1,
                        "max_objects": 1,
                        "graph_hops": 0,
                        "confirmation_token": disclosure["confirmation_token"],
                    },
                )
                self.assertEqual(409, replay.status_code, replay.text)
                self.assertEqual("stale_preflight", replay.json()["detail"]["code"])
                self.assertFalse(replay.json()["detail"]["provider_invoked"])
                self.assertEqual(1, provider.calls)

                receipts = client.get(
                    f"/v1/tenants/{tenant_id}/ai-transfer-receipts"
                )
                self.assertEqual(200, receipts.status_code, receipts.text)
                self.assertEqual(3, len(receipts.json()))
                self.assertNotIn("Show orders", receipts.text)

                mismatched_policy = client.put(
                    f"{base}/provider-egress-policies/fake",
                    json={
                        "allowed": True,
                        "maximum_classification": "internal",
                        "allowed_purposes": ["sql_proposal_generation"],
                        "data_residency": "eu",
                        "retention_mode": "provider_default",
                        "acknowledged": True,
                    },
                )
                self.assertEqual(200, mismatched_policy.status_code, mismatched_policy.text)
                mismatch = client.get(f"{base}/privacy/providers").json()[0]
                self.assertFalse(mismatch["deployment_matches_policy"])
                self.assertEqual("residency_mismatch", mismatch["decision_code"])


if __name__ == "__main__":
    unittest.main()
