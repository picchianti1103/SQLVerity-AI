from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.catalog.sqlverity_catalog.inference import SemanticInferenceService
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ExplainResult,
    LLMResponse,
    ReadOnlyResult,
    SQLProposal,
    TokenEstimate,
)
from packages.domain.sqlverity_domain.models import (
    DataSource,
    QueryRequest,
    QueryRequestState,
)
from packages.llm_gateway.sqlverity_llm_gateway import (
    LLMGateway,
    MetadataOnlyPolicyEngine,
    SchemaQuestionPolicyEngine,
)
from packages.query.sqlverity_query import (
    IntentCorrectionInterpreterService,
    QueryExecutionService,
    SQLGenerationService,
)
from packages.result_engine.sqlverity_result_engine import DeterministicResultProcessor
from packages.retrieval.sqlverity_retrieval import ContextBuilderService
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class APISQLProvider:
    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        if request["purpose"] == "intent_correction_interpretation":
            return LLMResponse(
                payload={
                    "entity_index": 0,
                    "term_to_remember": "orders",
                    "corrected_object_ref": "public.orders",
                    "confidence": 0.98,
                    "reason": "The correction confirms the current table mapping.",
                    "alternatives": [],
                    "ambiguities": [],
                    "needs_clarification": False,
                },
                model_id="api-correction-model",
                input_tokens=35,
                output_tokens=18,
                latency_ms=25,
            )
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
                            "reason": "Il nome della tabella coincide con il termine richiesto.",
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
            model_id="api-sql-model",
            input_tokens=45,
            output_tokens=20,
            latency_ms=30,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(input_tokens=50, output_tokens=25)

    def capabilities(self) -> Mapping[str, Any]:
        return {"structured_output": True}

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}


class APIReadOnlyExecutor:
    def explain(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> ExplainResult:
        return ExplainResult(
            plan={"Plan": {"Node Type": "Seq Scan", "Total Cost": 4.2}},
            estimated_total_cost=4.2,
            estimated_rows=1,
            elapsed_ms=2,
        )

    def execute_read_only(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
        max_rows: int,
        max_result_bytes: int,
    ) -> ReadOnlyResult:
        return ReadOnlyResult(
            columns=("id",),
            rows=({"id": 1},),
            row_count=1,
            truncated=False,
            truncation_reason=None,
            result_bytes=8,
            elapsed_ms=3,
        )

    def cancel(self, request_id: str) -> bool:
        return True


class APISemanticProvider:
    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        return LLMResponse(
            payload={
                "proposals": [
                    {
                        "object_ref": "public.orders",
                        "description": "Customer order records",
                        "confidence": 0.88,
                        "reason": "Inferred from the table name",
                    }
                ]
            },
            model_id="api-test-model",
            input_tokens=30,
            output_tokens=12,
            latency_ms=20,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(input_tokens=35, output_tokens=20)

    def capabilities(self) -> Mapping[str, Any]:
        return {"structured_output": True}

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}


class APITests(unittest.TestCase):
    def test_lifespan_loads_environment_llm_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            configured = {"openai": APISemanticProvider()}
            with (
                patch.dict(os.environ, api_test_environment(catalog_path)),
                patch(
                    "apps.api.main.load_llm_providers_from_environment",
                    return_value=configured,
                ) as provider_loader,
            ):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    response = client.get("/health")

                self.assertEqual(200, response.status_code)
                provider_loader.assert_called_once_with()

    def test_ddl_import_and_schema_explorer_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant_response = client.post("/v1/tenants", json={"name": "Acme"})
                    self.assertEqual(201, tenant_response.status_code)
                    tenant_id = tenant_response.json()["id"]

                    source_response = client.post(
                        f"/v1/tenants/{tenant_id}/data-sources",
                        json={
                            "name": "DDL catalog",
                            "source_type": "ddl_import",
                            "dialect": "postgresql",
                        },
                    )
                    self.assertEqual(201, source_response.status_code)
                    data_source_id = source_response.json()["id"]

                    import_response = client.post(
                        (
                            f"/v1/tenants/{tenant_id}/data-sources/"
                            f"{data_source_id}/imports/ddl"
                        ),
                        json={
                            "ddl": (
                                "CREATE TABLE public.events ("
                                "id UUID PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL); "
                                "COMMENT ON TABLE public.events IS 'Tracked events'"
                            )
                        },
                    )
                    self.assertEqual(201, import_response.status_code, import_response.text)
                    self.assertEqual(1, import_response.json()["catalog_version"])

                    schema_response = client.get(
                        f"/v1/tenants/{tenant_id}/data-sources/{data_source_id}/schema"
                    )
                    self.assertEqual(200, schema_response.status_code, schema_response.text)
                    schema = schema_response.json()
                    self.assertEqual("public.events", schema["objects"][0]["reference"])
                    self.assertEqual(
                        "Tracked events",
                        schema["objects"][0]["semantics"]["description"],
                    )
                    self.assertTrue(schema["objects"][0]["columns"][0]["is_primary_key"])

    def test_manual_import_preserves_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Manual catalog",
                            "source_type": "manual_schema",
                            "dialect": "postgresql",
                        },
                    ).json()
                    import_response = client.post(
                        (
                            f"/v1/tenants/{tenant['id']}/data-sources/"
                            f"{source['id']}/imports/manual"
                        ),
                        json={
                            "objects": [
                                {
                                    "schema_name": "public",
                                    "name": "customers",
                                    "kind": "table",
                                    "columns": [
                                        {
                                            "name": "email",
                                            "physical_type": "varchar(255)",
                                            "ordinal": 1,
                                            "nullable": False,
                                            "classification": "pii",
                                            "comment": "Customer email",
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                    self.assertEqual(201, import_response.status_code, import_response.text)

                    schema_response = client.get(
                        f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}/schema"
                    )
                    self.assertEqual(200, schema_response.status_code)
                    email = schema_response.json()["objects"][0]["columns"][0]
                    self.assertEqual("pii", email["classification"])
                    self.assertEqual("Customer email", email["semantics"]["description"])

    def test_semantic_correction_history_and_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Manual catalog",
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
                                    "comment": "Imported order records",
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
                    self.assertEqual(201, imported.status_code, imported.text)

                    schema = client.get(f"{base_path}/schema").json()
                    imported_semantics = schema["objects"][0]["semantics"]
                    self.assertIn("updated_at", imported_semantics)
                    correction_payload = {
                        "object_ref": "public.orders",
                        "description": "Validated customer orders",
                        "reason": "Aligned with the business glossary",
                        "expected_updated_at": imported_semantics["updated_at"],
                    }
                    corrected = client.post(
                        f"{base_path}/semantics/corrections",
                        json=correction_payload,
                    )

                    self.assertEqual(201, corrected.status_code, corrected.text)
                    self.assertEqual("confirmed", corrected.json()["resolution"]["status"])
                    self.assertEqual(
                        "bootstrap-admin",
                        corrected.json()["definition"]["actor_id"],
                    )
                    history = client.get(
                        f"{base_path}/semantics/history",
                        params={"object_ref": "public.orders"},
                    )
                    self.assertEqual(200, history.status_code, history.text)
                    self.assertEqual(2, len(history.json()))
                    self.assertEqual(1, sum(item["selected"] for item in history.json()))

                    stale = client.post(
                        f"{base_path}/semantics/corrections",
                        json={**correction_payload, "description": "Stale overwrite"},
                    )
                    self.assertEqual(409, stale.status_code, stale.text)

    def test_semantic_inference_enters_review_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Manual catalog",
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
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                    self.assertEqual(201, imported.status_code, imported.text)

                    unconfigured = client.post(
                        f"{base_path}/semantics/inferences",
                        json={"provider_id": "missing"},
                    )
                    self.assertEqual(503, unconfigured.status_code, unconfigured.text)

                    repository = cast(SQLiteCatalogRepository, app.state.catalog)
                    gateway = LLMGateway(
                        {"fake": APISemanticProvider()},
                        MetadataOnlyPolicyEngine(
                            allowed_provider_ids=frozenset({"fake"})
                        ),
                        repository,
                    )
                    app.state.semantic_inference = SemanticInferenceService(
                        repository,
                        gateway,
                    )
                    inferred = client.post(
                        f"{base_path}/semantics/inferences",
                        json={"provider_id": "fake"},
                    )

                    self.assertEqual(201, inferred.status_code, inferred.text)
                    self.assertEqual("api-test-model", inferred.json()["model_id"])
                    self.assertEqual(1, len(inferred.json()["proposals"]))
                    self.assertEqual(
                        "inferred",
                        inferred.json()["proposals"][0]["resolution"]["status"],
                    )
                    review = client.get(f"{base_path}/semantic-reviews")
                    self.assertEqual(200, review.status_code, review.text)
                    self.assertEqual("public.orders", review.json()[0]["object_ref"])

    def test_context_preview_and_validated_sql_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Manual catalog",
                            "source_type": "manual_schema",
                            "dialect": "postgresql",
                            "capabilities": [
                                "explain",
                                "execute_read_only",
                                "cancel",
                            ],
                            "connection_secret_ref": "env://SQLVERITY_TEST_DB",
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
                                        }
                                    ],
                                },
                                {
                                    "schema_name": "public",
                                    "name": "audit_log",
                                    "kind": "table",
                                    "columns": [
                                        {
                                            "name": "id",
                                            "physical_type": "bigint",
                                            "ordinal": 1,
                                            "nullable": False,
                                        }
                                    ],
                                },
                            ]
                        },
                    )
                    self.assertEqual(201, imported.status_code, imported.text)

                    preview = client.post(
                        f"{base_path}/context/previews",
                        json={
                            "query": "Show orders",
                            "max_seed_objects": 1,
                            "max_objects": 1,
                        },
                    )
                    self.assertEqual(200, preview.status_code, preview.text)
                    self.assertEqual("public.orders", preview.json()["objects"][0]["reference"])
                    self.assertEqual(1, preview.json()["omitted_object_count"])

                    unavailable = client.post(
                        f"{base_path}/sql/proposals",
                        json={
                            "provider_id": "missing",
                            "query": "Show orders",
                            "question_classification": "internal",
                            "max_seed_objects": 1,
                            "max_objects": 1,
                        },
                    )
                    self.assertEqual(503, unavailable.status_code, unavailable.text)

                    repository = cast(SQLiteCatalogRepository, app.state.catalog)
                    gateway = LLMGateway(
                        {"fake": APISQLProvider()},
                        SchemaQuestionPolicyEngine(
                            allowed_provider_ids=frozenset({"fake"})
                        ),
                        repository,
                    )
                    app.state.sql_generation = SQLGenerationService(
                        ContextBuilderService(repository),
                        gateway,
                        PostgreSQLSQLValidator(),
                        repository,
                    )
                    app.state.intent_correction_interpreter = (
                        IntentCorrectionInterpreterService(
                            repository,
                            gateway,
                            app.state.intent_memory,
                        )
                    )
                    generated = client.post(
                        f"{base_path}/sql/proposals",
                        json={
                            "provider_id": "fake",
                            "query": "Show orders",
                            "question_classification": "internal",
                            "max_seed_objects": 1,
                            "max_objects": 1,
                        },
                    )

                    self.assertEqual(201, generated.status_code, generated.text)
                    self.assertEqual("api-sql-model", generated.json()["model_id"])
                    self.assertEqual("accepted", generated.json()["validation_status"])
                    self.assertEqual("ready_for_preview", generated.json()["state"])
                    self.assertEqual("maximum_privacy", generated.json()["privacy_mode"])
                    self.assertEqual("deterministic", generated.json()["generation_strategy"])
                    self.assertEqual(1, generated.json()["generation_attempt_count"])
                    self.assertIsNone(generated.json()["fallback_reason"])
                    self.assertEqual(
                        "record_list",
                        generated.json()["interpretation"]["kind"],
                    )
                    self.assertEqual(
                        "public.orders",
                        generated.json()["interpretation"]["entities"][0]["object_ref"],
                    )
                    self.assertTrue(generated.json()["ready_for_preview"])
                    self.assertFalse(generated.json()["ready_for_execution"])

                    semantic_retry = client.post(
                        f"{base_path}/sql/proposals",
                        json={
                            "provider_id": "fake",
                            "query": "Show orders",
                            "question_classification": "internal",
                            "max_seed_objects": 1,
                            "max_objects": 1,
                            "privacy_mode": "governed_semantic",
                            "force_semantic": True,
                        },
                    )
                    self.assertEqual(201, semantic_retry.status_code, semantic_retry.text)
                    self.assertEqual(
                        "semantic_user_retry",
                        semantic_retry.json()["generation_strategy"],
                    )
                    self.assertEqual(
                        "governed_semantic",
                        semantic_retry.json()["privacy_mode"],
                    )

                    request_id = generated.json()["request_id"]
                    request_path = f"{base_path}/query-requests/{request_id}"
                    conversational = client.post(
                        f"{request_path}/intent-corrections/from-text",
                        json={
                            "provider_id": "fake",
                            "correction_text": (
                                "Confermo: per ordini intendo la tabella orders."
                            ),
                            "correction_classification": "internal",
                            "current_entities": [
                                {
                                    "term": "orders",
                                    "role": "primary_table",
                                    "object_ref": "public.orders",
                                }
                            ],
                        },
                    )
                    self.assertEqual(201, conversational.status_code, conversational.text)
                    self.assertEqual(
                        "public.orders",
                        conversational.json()["interpretation"]["corrected_object_ref"],
                    )
                    self.assertFalse(
                        conversational.json()["interpretation"]["needs_clarification"]
                    )
                    self.assertEqual(
                        "created",
                        conversational.json()["memory_correction"]["memory_action"],
                    )
                    self.assertFalse(
                        conversational.json()["memory_correction"]["requires_regeneration"]
                    )

                    app.state.query_execution = QueryExecutionService(
                        repository,
                        PostgreSQLSQLValidator(),
                        SchemaQuestionPolicyEngine(),
                        {"postgresql": APIReadOnlyExecutor()},
                        DeterministicResultProcessor(),
                    )
                    explained = client.post(f"{request_path}/explain")
                    self.assertEqual(200, explained.status_code, explained.text)
                    self.assertEqual(4.2, explained.json()["estimated_total_cost"])

                    premature = client.post(f"{request_path}/executions")
                    self.assertEqual(409, premature.status_code, premature.text)

                    approved = client.post(
                        f"{request_path}/approval",
                        json={},
                    )
                    self.assertEqual(200, approved.status_code, approved.text)
                    self.assertEqual("approved", approved.json()["state"])

                    executed = client.post(f"{request_path}/executions")
                    self.assertEqual(200, executed.status_code, executed.text)
                    self.assertEqual(
                        "completed",
                        executed.json()["query_request"]["state"],
                    )
                    self.assertEqual(1, executed.json()["result"]["row_count"])
                    self.assertEqual("single_value", executed.json()["answer"]["shape"])
                    self.assertEqual(
                        "deterministic_local",
                        executed.json()["privacy"]["processing_mode"],
                    )
                    self.assertFalse(
                        executed.json()["privacy"]["raw_rows_sent_to_llm"]
                    )
                    self.assertEqual(
                        "api-sql-model",
                        executed.json()["provenance"]["model_id"],
                    )

                    correction_path = f"{request_path}/intent-corrections"
                    remembered = client.post(
                        correction_path,
                        json={
                            "term": "order identifier",
                            "role": "selected_column",
                            "previous_object_ref": "public.orders.id",
                            "corrected_object_ref": "public.orders.id",
                            "reason": "Confirmed by the data steward.",
                        },
                    )
                    self.assertEqual(201, remembered.status_code, remembered.text)
                    self.assertEqual("created", remembered.json()["memory_action"])
                    self.assertFalse(remembered.json()["requires_regeneration"])
                    self.assertEqual(
                        ["public.orders.id"],
                        remembered.json()["resolution"]["object_refs"],
                    )

                    modified = client.post(
                        correction_path,
                        json={
                            "term": "order identifier",
                            "role": "selected_column",
                            "previous_object_ref": "public.orders.id",
                            "corrected_object_ref": "public.audit_log.id",
                            "reason": "The first remembered mapping was wrong.",
                        },
                    )
                    self.assertEqual(201, modified.status_code, modified.text)
                    self.assertEqual("updated", modified.json()["memory_action"])
                    self.assertTrue(modified.json()["requires_regeneration"])
                    self.assertEqual(
                        ["public.audit_log.id"],
                        modified.json()["resolution"]["object_refs"],
                    )
                    concept_key = modified.json()["resolution"]["concept_key"]
                    history = client.get(
                        f"{base_path}/business-concepts/{concept_key}/history"
                    )
                    self.assertEqual(200, history.status_code, history.text)
                    self.assertEqual(2, len(history.json()))

    def test_finops_configuration_summary_and_execution_policy_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "FinOps"}).json()
                    tenant_path = f"/v1/tenants/{tenant['id']}"

                    pricing = client.post(
                        f"{tenant_path}/finops/model-pricing",
                        json={
                            "provider_id": "provider-a",
                            "model_id": "model-a",
                            "valid_from": "2026-01-01T00:00:00Z",
                            "valid_to": "2027-01-01T00:00:00Z",
                            "currency": "USD",
                            "token_unit": 1_000_000,
                            "input_price_per_unit": "2",
                            "cached_input_price_per_unit": "0.5",
                            "output_price_per_unit": "8",
                            "source_version": "provider-2026-01",
                        },
                    )
                    self.assertEqual(201, pricing.status_code, pricing.text)
                    self.assertEqual("provider-2026-01", pricing.json()["source_version"])

                    budget = client.post(
                        f"{tenant_path}/finops/budgets",
                        json={
                            "currency": "USD",
                            "amount": "25",
                            "valid_from": "2026-01-01T00:00:00Z",
                            "valid_to": "2027-01-01T00:00:00Z",
                        },
                    )
                    self.assertEqual(201, budget.status_code, budget.text)
                    self.assertEqual("monthly", budget.json()["period"])

                    summary = client.get(f"{tenant_path}/finops/summary?currency=USD")
                    self.assertEqual(200, summary.status_code, summary.text)
                    self.assertEqual("0", summary.json()["total_cost"])
                    self.assertEqual("25", summary.json()["budget_amount"])
                    self.assertEqual([], summary.json()["breakdown"])

                    source = client.post(
                        f"{tenant_path}/data-sources",
                        json={
                            "name": "Warehouse",
                            "source_type": "direct_db",
                            "dialect": "postgresql",
                        },
                    ).json()
                    policy_path = (
                        f"{tenant_path}/data-sources/{source['id']}"
                        "/execution-cost-policy"
                    )
                    policy = client.put(
                        policy_path,
                        json={
                            "max_total_cost": 500,
                            "max_estimated_rows": 10_000,
                            "require_explain": True,
                        },
                    )
                    self.assertEqual(200, policy.status_code, policy.text)
                    self.assertEqual(500, policy.json()["max_total_cost"])
                    loaded_policy = client.get(policy_path)
                    self.assertEqual(200, loaded_policy.status_code, loaded_policy.text)
                    self.assertTrue(loaded_policy.json()["require_explain"])

                    self.assertEqual(
                        1,
                        len(client.get(f"{tenant_path}/finops/model-pricing").json()),
                    )
                    self.assertEqual(
                        1,
                        len(client.get(f"{tenant_path}/finops/budgets").json()),
                    )

    def test_authorized_query_definition_api_builds_virtual_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post(
                        "/v1/tenants",
                        json={"name": "Authorized API"},
                    ).json()
                    unsafe_source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Unsafe authorized source",
                            "source_type": "authorized_query",
                            "dialect": "postgresql",
                            "capabilities": ["introspect"],
                        },
                    )
                    self.assertEqual(422, unsafe_source.status_code, unsafe_source.text)
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "External sales",
                            "source_type": "authorized_query",
                            "dialect": "postgresql",
                            "capabilities": ["explain", "execute_read_only"],
                            "connection_secret_ref": "env://AUTHORIZED_QUERY_DB",
                        },
                    ).json()
                    base_path = (
                        f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}"
                    )
                    definitions_path = f"{base_path}/authorized-query/definitions"
                    registered = client.post(
                        definitions_path,
                        json={
                            "virtual_schema": "authorized",
                            "virtual_name": "external_sales",
                            "description": "Governed external sales",
                            "base_sql": (
                                "SELECT customer_key, sale_date, category, net_amount "
                                "FROM reporting.external_sales_view "
                                "WHERE sale_date >= CAST(:start_date AS DATE)"
                            ),
                            "parameters": [
                                {
                                    "name": "start_date",
                                    "physical_type": "date",
                                }
                            ],
                            "output_columns": [
                                {
                                    "name": "customer_key",
                                    "physical_type": "text",
                                    "nullable": False,
                                    "classification": "confidential",
                                },
                                {
                                    "name": "sale_date",
                                    "physical_type": "date",
                                    "nullable": False,
                                },
                                {
                                    "name": "category",
                                    "physical_type": "text",
                                    "nullable": False,
                                },
                                {
                                    "name": "net_amount",
                                    "physical_type": "numeric",
                                    "nullable": False,
                                },
                            ],
                            "allow_filtering": True,
                            "allow_aggregation": True,
                        },
                    )
                    self.assertEqual(201, registered.status_code, registered.text)
                    body = registered.json()
                    self.assertEqual(1, body["definition"]["version"])
                    self.assertEqual(
                        "authorized.external_sales",
                        body["definition"]["virtual_object_ref"],
                    )
                    self.assertIn(
                        "%(start_date)s",
                        body["definition"]["normalized_base_sql"],
                    )

                    definitions = client.get(definitions_path)
                    self.assertEqual(200, definitions.status_code, definitions.text)
                    self.assertEqual(1, len(definitions.json()))
                    schema = client.get(f"{base_path}/schema")
                    self.assertEqual(200, schema.status_code, schema.text)
                    self.assertEqual("virtual_query", schema.json()["objects"][0]["kind"])
                    self.assertEqual(
                        "confidential",
                        schema.json()["objects"][0]["columns"][0]["classification"],
                    )

                    repository = cast(SQLiteCatalogRepository, app.state.catalog)
                    validator = PostgreSQLSQLValidator()
                    proposal = SQLProposal(
                        intent="data_query",
                        sql="SELECT category FROM authorized.external_sales",
                        dialect="postgresql",
                        tables=("authorized.external_sales",),
                        columns=("authorized.external_sales.category",),
                    )
                    validation = validator.validate(
                        proposal,
                        allowed_tables=frozenset(proposal.tables),
                        allowed_columns=frozenset(proposal.columns),
                        max_rows=100,
                    )
                    assert validation.normalized_sql is not None
                    query_request = repository.create_query_request(
                        QueryRequest(
                            tenant_id=tenant["id"],
                            data_source_id=source["id"],
                            catalog_version_id=body["definition"]["catalog_version_id"],
                            sql_text=proposal.sql,
                            normalized_sql=validation.normalized_sql,
                            referenced_tables=validation.referenced_tables,
                            referenced_columns=validation.referenced_columns,
                            validation_issue_codes=tuple(
                                issue.code for issue in validation.issues
                            ),
                            state=QueryRequestState.READY_FOR_PREVIEW,
                        )
                    )
                    app.state.query_execution = QueryExecutionService(
                        repository,
                        validator,
                        SchemaQuestionPolicyEngine(),
                        {"postgresql": APIReadOnlyExecutor()},
                        DeterministicResultProcessor(),
                        max_rows=100,
                    )
                    request_path = f"{base_path}/query-requests/{query_request.id}"
                    bindings = {"parameters": {"start_date": "2026-01-01"}}
                    explained = client.post(
                        f"{request_path}/explain",
                        json=bindings,
                    )
                    self.assertEqual(200, explained.status_code, explained.text)
                    approved = client.post(
                        f"{request_path}/approval",
                        json=bindings,
                    )
                    self.assertEqual(200, approved.status_code, approved.text)
                    self.assertEqual(["start_date"], approved.json()["parameter_names"])
                    changed = client.post(
                        f"{request_path}/executions",
                        json={"parameters": {"start_date": "2026-02-01"}},
                    )
                    self.assertEqual(403, changed.status_code, changed.text)
                    executed = client.post(
                        f"{request_path}/executions",
                        json=bindings,
                    )
                    self.assertEqual(200, executed.status_code, executed.text)
                    self.assertEqual(
                        ["start_date"],
                        executed.json()["provenance"]["parameter_names"],
                    )

                    invalid = client.post(
                        definitions_path,
                        json={
                            "virtual_name": "unsafe_surface",
                            "description": "Must fail",
                            "base_sql": "DELETE FROM reporting.external_sales_view",
                            "output_columns": [
                                {"name": "id", "physical_type": "bigint"}
                            ],
                        },
                    )
                    self.assertEqual(422, invalid.status_code, invalid.text)


if __name__ == "__main__":
    unittest.main()
