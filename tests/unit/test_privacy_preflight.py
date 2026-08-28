from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    LLMResponse,
    SchemaObjectSnapshot,
    TokenEstimate,
)
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceType,
    ObjectKind,
    ProviderEgressPolicy,
    ProviderRetentionMode,
    utc_now,
)
from packages.llm_gateway.sqlverity_llm_gateway import (
    LLMGateway,
    SchemaQuestionPolicyEngine,
)
from packages.query.sqlverity_query import (
    PreflightConfirmationError,
    PreflightConfirmationManager,
    SQLGenerationPreflight,
    SQLGenerationService,
    policy_acknowledgement_digest,
)
from packages.retrieval.sqlverity_retrieval import ContextBuilderService
from packages.security.sqlverity_security import ServerSideTextClassifier
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator


class PrivacyProviderSpy:
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
            model_id="privacy-model-1",
            input_tokens=100,
            output_tokens=30,
            latency_ms=25,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(input_tokens=110, output_tokens=40)

    def capabilities(self) -> Mapping[str, Any]:
        return {"structured_output": True, "model_id": "privacy-model-1"}

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}


class PrivacyPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Privacy tenant")
        self.source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.source.id,
            DataSourceSnapshot(
                data_source_id=self.source.id,
                dialect="postgresql",
                objects=(
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="orders",
                        kind=ObjectKind.TABLE,
                        columns=(ColumnSnapshot("id", "bigint", 1, False),),
                    ),
                ),
            ),
        )
        self.provider = PrivacyProviderSpy()
        self._store_policy(Classification.INTERNAL)
        self.service = self._service()

    def tearDown(self) -> None:
        self.repository.close()

    def test_preflight_is_provider_free_and_receipt_omits_content(self) -> None:
        preflight = self.service.preflight(
            tenant_id=self.tenant.id,
            data_source_id=self.source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
            actor_id="analyst-1",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        self.assertTrue(preflight.allowed)
        self.assertFalse(preflight.provider_invoked)
        self.assertIsNotNone(preflight.confirmation_token)
        self.assertFalse(preflight.review_required)
        self.assertEqual(0, self.provider.calls)
        receipts = self.repository.list_ai_transfer_receipts(self.tenant.id)
        self.assertEqual(1, len(receipts))
        self.assertFalse(receipts[0].provider_invoked)
        self.assertNotIn("Show orders", str(receipts[0]))
        self.assertNotIn("Show orders", str(self.repository.audit_events(self.tenant.id)))

    def test_confirmation_is_bound_and_single_use(self) -> None:
        preflight = self._preflight()
        assert preflight.confirmation_token is not None
        run = self.service.generate(
            tenant_id=self.tenant.id,
            data_source_id=self.source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
            actor_id="analyst-1",
            confirmation_token=preflight.confirmation_token,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        self.assertEqual(1, self.provider.calls)
        self.assertIsNotNone(run.transfer_receipt)
        assert run.transfer_receipt is not None
        self.assertTrue(run.transfer_receipt.provider_invoked)
        self.assertEqual(run.usage.id, run.transfer_receipt.llm_usage_event_id)
        with self.assertRaises(PreflightConfirmationError):
            self.service.generate(
                tenant_id=self.tenant.id,
                data_source_id=self.source.id,
                provider_id="fake",
                question="Show orders",
                question_classification=Classification.INTERNAL,
                actor_id="analyst-1",
                confirmation_token=preflight.confirmation_token,
                max_seed_objects=1,
                max_objects=1,
                graph_hops=0,
            )
        self.assertEqual(1, self.provider.calls)

    def test_shared_confirmation_store_prevents_cross_replica_replay(self) -> None:
        signing_key = b"shared-preflight-signing-key-0001"
        issuer = PreflightConfirmationManager(
            signing_key,
            confirmation_store=self.repository,
        )
        consumer = PreflightConfirmationManager(
            signing_key,
            confirmation_store=self.repository,
        )
        binding = {"tenant_id": self.tenant.id, "manifest_digest": "a" * 64}

        token, _expires_at = issuer.issue(binding)
        consumer.consume(token, binding)

        with self.assertRaises(PreflightConfirmationError):
            issuer.consume(token, binding)

    def test_question_edit_invalidates_confirmation(self) -> None:
        preflight = self._preflight()
        assert preflight.confirmation_token is not None
        with self.assertRaises(PreflightConfirmationError):
            self.service.generate(
                tenant_id=self.tenant.id,
                data_source_id=self.source.id,
                provider_id="fake",
                question="Show all orders",
                question_classification=Classification.INTERNAL,
                actor_id="analyst-1",
                confirmation_token=preflight.confirmation_token,
                max_seed_objects=1,
                max_objects=1,
                graph_hops=0,
            )
        self.assertEqual(0, self.provider.calls)

    def test_server_elevation_blocks_required_question_without_echo(self) -> None:
        question = "Show orders for +39 333 123 4567"
        assessment = ServerSideTextClassifier().classify(
            question,
            Classification.INTERNAL,
        )
        preflight = self.service.preflight(
            tenant_id=self.tenant.id,
            data_source_id=self.source.id,
            provider_id="fake",
            question=question,
            question_classification=assessment.effective,
            declared_classification=assessment.declared,
            detected_classification=assessment.detected,
            detection_reason_codes=assessment.reasons,
            actor_id="analyst-1",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        self.assertFalse(preflight.allowed)
        self.assertEqual("required_prompt_content_redacted", preflight.decision_code)
        self.assertEqual(("phone_number",), preflight.detection_reason_codes)
        self.assertIsNone(preflight.confirmation_token)
        self.assertEqual(0, self.provider.calls)
        self.assertNotIn(question, str(self.repository.audit_events(self.tenant.id)))

    def _preflight(self) -> SQLGenerationPreflight:
        return self.service.preflight(
            tenant_id=self.tenant.id,
            data_source_id=self.source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
            actor_id="analyst-1",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

    def _service(self) -> SQLGenerationService:
        metadata = {
            "fake": {
                "data_residency": "eu",
                "retention_mode": "zero",
                "deployment_type": "external_cloud",
                "model_id": "privacy-model-1",
            }
        }
        gateway = LLMGateway(
            {"fake": self.provider},
            SchemaQuestionPolicyEngine(
                allowed_provider_ids=frozenset({"fake"}),
                provider_policy_repository=self.repository,
                provider_metadata=metadata,
                require_explicit_provider_policy=True,
            ),
            self.repository,
        )
        return SQLGenerationService(
            ContextBuilderService(self.repository),
            gateway,
            PostgreSQLSQLValidator(),
            self.repository,
            confirmation_manager=PreflightConfirmationManager(b"x" * 32),
            receipt_recorder=self.repository,
        )

    def _store_policy(self, maximum: Classification) -> None:
        scope = "tenant"
        self.repository.upsert_provider_egress_policy(
            ProviderEgressPolicy(
                tenant_id=self.tenant.id,
                provider_id="fake",
                allowed_purposes=("sql_proposal_generation",),
                maximum_classification=maximum,
                data_residency="eu",
                retention_mode=ProviderRetentionMode.ZERO,
                acknowledgement_digest=policy_acknowledgement_digest(
                    provider_id="fake",
                    model_id="privacy-model-1",
                    allowed=True,
                    allowed_purposes=("sql_proposal_generation",),
                    maximum_classification=maximum,
                    data_residency="eu",
                    retention_mode="zero",
                    scope=scope,
                    deployment_type="external_cloud",
                ),
                acknowledged_by="admin-1",
                acknowledged_at=utc_now(),
            )
        )


if __name__ == "__main__":
    unittest.main()
