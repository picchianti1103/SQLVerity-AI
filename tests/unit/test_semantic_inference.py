from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from packages.catalog.sqlverity_catalog.governance import SemanticGovernanceService
from packages.catalog.sqlverity_catalog.inference import (
    InvalidSemanticInferenceOutputError,
    SemanticInferenceNoTargetsError,
    SemanticInferenceService,
)
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
    EpistemicStatus,
    ObjectKind,
    SemanticDefinition,
)
from packages.llm_gateway.sqlverity_llm_gateway import LLMGateway, MetadataOnlyPolicyEngine


class SemanticProvider:
    def __init__(self, proposals: list[dict[str, object]]) -> None:
        self.proposals = proposals
        self.requests: list[Mapping[str, Any]] = []

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            payload={"proposals": self.proposals},
            model_id="semantic-model-1",
            input_tokens=120,
            output_tokens=55,
            latency_ms=80,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(input_tokens=130, output_tokens=70, estimated_cost="0.004")

    def capabilities(self) -> Mapping[str, Any]:
        return {"structured_output": True}

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}


class SemanticInferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        self.report = CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.data_source.id,
            DataSourceSnapshot(
                data_source_id=self.data_source.id,
                dialect="postgresql",
                objects=(
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="orders",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "bigint", 1, False, is_primary_key=True),
                            ColumnSnapshot(
                                "customer_email",
                                "varchar(255)",
                                2,
                                False,
                                classification=Classification.PII,
                            ),
                            ColumnSnapshot("created_at", "timestamptz", 3, False),
                        ),
                    ),
                ),
            ),
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_inferences_are_persisted_and_enter_the_review_queue(self) -> None:
        provider = SemanticProvider(
            [
                self._proposal("public.orders", "Customer order records", 0.86),
                self._proposal("public.orders.id", "Order identifier", 0.94),
                self._proposal(
                    "public.orders.created_at",
                    "Time when the order was created",
                    0.91,
                ),
            ]
        )
        service = self._service(provider)

        result = service.infer_missing_descriptions(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
        )

        self.assertEqual(3, len(result.proposals))
        self.assertEqual(("public.orders.customer_email",), result.redacted_object_refs)
        self.assertTrue(
            all(item.resolution.status is EpistemicStatus.INFERRED for item in result.proposals)
        )
        queue = SemanticGovernanceService(self.repository).list_review_queue(
            self.tenant.id,
            self.data_source.id,
        )
        self.assertEqual(3, len(queue))
        self.assertNotIn(
            "public.orders.customer_email",
            self._requested_ids(provider.requests[0]),
        )
        usage = self.repository.list_llm_usage_events(self.tenant.id)
        self.assertEqual(1, len(usage))
        self.assertEqual("semantic_description_inference", usage[0].purpose)
        self.assertEqual("0.004", usage[0].estimated_cost)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(  # noqa: SLF001 - verifies DB enforcement
                "DELETE FROM llm_usage_events WHERE id = ?",
                (usage[0].id,),
            )

    def test_policy_redacted_reference_in_output_rejects_entire_response(self) -> None:
        provider = SemanticProvider(
            [
                self._proposal(
                    "public.orders.customer_email",
                    "Customer email address",
                    0.99,
                )
            ]
        )

        with self.assertRaises(InvalidSemanticInferenceOutputError):
            self._service(provider).infer_missing_descriptions(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
            )

        self.assertEqual(
            (),
            self.repository.list_semantic_definitions(
                self.tenant.id,
                self.data_source.id,
                "public.orders",
            ),
        )
        self.assertEqual(1, len(self.repository.list_llm_usage_events(self.tenant.id)))

    def test_imported_semantics_are_not_sent_as_inference_targets(self) -> None:
        self._import_semantics("public.orders", "Imported order records")
        provider = SemanticProvider(
            [self._proposal("public.orders.id", "Order identifier", 0.94)]
        )

        self._service(provider).infer_missing_descriptions(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
        )

        self.assertNotIn("public.orders", self._requested_ids(provider.requests[0]))
        table_history = self.repository.list_semantic_definitions(
            self.tenant.id,
            self.data_source.id,
            "public.orders",
        )
        self.assertEqual(1, len(table_history))
        self.assertEqual(EpistemicStatus.IMPORTED, table_history[0].status)

    def test_run_fails_before_provider_when_every_reference_is_governed(self) -> None:
        for object_ref in (
            "public.orders",
            "public.orders.id",
            "public.orders.customer_email",
            "public.orders.created_at",
        ):
            self._import_semantics(object_ref, f"Imported description for {object_ref}")
        provider = SemanticProvider([])

        with self.assertRaises(SemanticInferenceNoTargetsError):
            self._service(provider).infer_missing_descriptions(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
            )

        self.assertEqual([], provider.requests)
        self.assertEqual((), self.repository.list_llm_usage_events(self.tenant.id))

    def _service(self, provider: SemanticProvider) -> SemanticInferenceService:
        gateway = LLMGateway(
            {"fake": provider},
            MetadataOnlyPolicyEngine(allowed_provider_ids=frozenset({"fake"})),
            self.repository,
        )
        return SemanticInferenceService(self.repository, gateway)

    def _import_semantics(self, object_ref: str, description: str) -> None:
        self.repository.propose_semantic_definition(
            SemanticDefinition(
                tenant_id=self.tenant.id,
                catalog_version_id=self.report.catalog_version_id,
                object_ref=object_ref,
                description=description,
                status=EpistemicStatus.IMPORTED,
                source="test:imported",
                confidence=1.0,
            )
        )

    @staticmethod
    def _proposal(
        object_ref: str,
        description: str,
        confidence: float,
    ) -> dict[str, object]:
        return {
            "object_ref": object_ref,
            "description": description,
            "confidence": confidence,
            "reason": "Inferred from schema identifiers and types",
        }

    @staticmethod
    def _requested_ids(request: Mapping[str, Any]) -> frozenset[str]:
        prompt_input = request["input"]
        assert isinstance(prompt_input, Mapping)
        items = prompt_input["items"]
        assert isinstance(items, Sequence)
        return frozenset(str(item["id"]) for item in items if isinstance(item, Mapping))


if __name__ == "__main__":
    unittest.main()
