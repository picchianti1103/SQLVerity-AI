from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from packages.catalog.sqlverity_catalog.business_concepts import BusinessConceptService
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
    QueryRequest,
    QueryRequestState,
)
from packages.llm_gateway.sqlverity_llm_gateway import (
    LLMGateway,
    SchemaQuestionPolicyEngine,
)
from packages.query.sqlverity_query import (
    CurrentIntentEntity,
    IntentCorrectionInterpreterService,
    IntentMemoryQueryNotFoundError,
    IntentMemoryReferenceError,
    IntentMemoryService,
    IntentMemoryStaleCatalogError,
    InvalidIntentCorrectionOutputError,
)
from packages.retrieval.sqlverity_retrieval import ContextBuilderService


class IntentCorrectionProvider:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.requests: list[Mapping[str, Any]] = []

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            payload=self.payload,
            model_id="correction-model-1",
            input_tokens=120,
            output_tokens=35,
            latency_ms=60,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(input_tokens=130, output_tokens=45, estimated_cost="0.004")

    def capabilities(self) -> Mapping[str, Any]:
        return {"structured_output": True}

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}


class IntentMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Generic transport catalog",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        self.version_id = self._ingest()
        self.concepts = BusinessConceptService(self.repository)
        self.memory = IntentMemoryService(self.repository, self.concepts)

    def tearDown(self) -> None:
        self.repository.close()

    def test_correction_creates_memory_cancels_stale_proposal_and_seeds_context(self) -> None:
        query_request = self._query_request()

        result = self.memory.correct_mapping(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=query_request.id,
            term="route name",
            role="selected_column",
            previous_object_ref="public.routes.description",
            corrected_object_ref="public.routes.label",
            actor_id="steward-1",
            reason="Label is the governed display name.",
        )

        self.assertEqual("created", result.memory_action)
        self.assertEqual(("public.routes.label",), result.resolution.object_refs)
        self.assertEqual(
            Classification.CONFIDENTIAL,
            result.resolution.content_classification,
        )
        self.assertTrue(result.requires_regeneration)
        self.assertEqual(QueryRequestState.CANCELLED, result.query_request_state)
        stored_request = self.repository.get_query_request(self.tenant.id, query_request.id)
        assert stored_request is not None
        self.assertEqual(QueryRequestState.CANCELLED, stored_request.state)

        context = ContextBuilderService(
            self.repository,
            business_concepts=self.concepts,
        ).build(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query="show the route name",
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )
        self.assertEqual("public.routes", context.objects[0].reference)
        self.assertEqual(("public.routes.label",), context.business_concepts[0].object_refs)

    def test_existing_memory_is_modified_with_immutable_history(self) -> None:
        first_request = self._query_request()
        first = self.memory.correct_mapping(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=first_request.id,
            term="route name",
            role="selected_column",
            previous_object_ref="public.routes.description",
            corrected_object_ref="public.routes.label",
            actor_id="steward-1",
        )
        second_request = self._query_request()

        second = self.memory.correct_mapping(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=second_request.id,
            term="route name",
            role="selected_column",
            previous_object_ref="public.routes.label",
            corrected_object_ref="public.routes.code",
            actor_id="steward-2",
            reason="The upstream code is the accepted business name.",
        )

        self.assertEqual("updated", second.memory_action)
        self.assertEqual(first.resolution.concept_key, second.resolution.concept_key)
        self.assertEqual(("public.routes.code",), second.resolution.object_refs)
        history = self.concepts.history(
            self.tenant.id,
            self.data_source.id,
            second.resolution.concept_key,
        )
        self.assertEqual(2, len(history))
        selected = next(entry for entry in history if entry.selected)
        superseded = next(entry for entry in history if not entry.selected)
        self.assertEqual(("public.routes.code",), selected.definition.object_refs)
        self.assertEqual(("public.routes.label",), superseded.definition.object_refs)

    def test_confirmation_of_same_mapping_keeps_query_usable(self) -> None:
        query_request = self._query_request()

        result = self.memory.correct_mapping(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=query_request.id,
            term="route label",
            role="selected_column",
            previous_object_ref="public.routes.label",
            corrected_object_ref="public.routes.label",
            actor_id="steward-1",
        )

        self.assertFalse(result.requires_regeneration)
        self.assertEqual(QueryRequestState.READY_FOR_PREVIEW, result.query_request_state)

    def test_role_catalog_version_and_tenant_boundaries_fail_closed(self) -> None:
        query_request = self._query_request()
        with self.assertRaises(IntentMemoryReferenceError):
            self.memory.correct_mapping(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=query_request.id,
                term="route name",
                role="primary_table",
                previous_object_ref=None,
                corrected_object_ref="public.routes.label",
                actor_id="steward-1",
            )

        other_tenant = self.repository.create_tenant("Other")
        with self.assertRaises(IntentMemoryQueryNotFoundError):
            self.memory.correct_mapping(
                tenant_id=other_tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=query_request.id,
                term="route name",
                role="selected_column",
                previous_object_ref="public.routes.description",
                corrected_object_ref="public.routes.label",
                actor_id="steward-1",
            )

        self._ingest()
        with self.assertRaises(IntentMemoryStaleCatalogError):
            self.memory.correct_mapping(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=query_request.id,
                term="route name",
                role="selected_column",
                previous_object_ref="public.routes.description",
                corrected_object_ref="public.routes.label",
                actor_id="steward-1",
            )

    def test_free_text_correction_is_grounded_and_updates_memory(self) -> None:
        query_request = self._query_request()
        provider = IntentCorrectionProvider(
            self._correction_payload(
                corrected_object_ref="public.routes.label",
                confidence=0.93,
            )
        )

        result = self._interpreter(provider).interpret_and_apply(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=query_request.id,
            provider_id="fake",
            correction_text=(
                "No, per route name intendo la colonna label, non description."
            ),
            correction_classification=Classification.INTERNAL,
            current_entities=self._current_entities(),
            actor_id="steward-1",
        )

        self.assertFalse(result.interpretation.needs_clarification)
        self.assertEqual("public.routes.label", result.interpretation.corrected_object_ref)
        self.assertIsNotNone(result.memory_correction)
        assert result.memory_correction is not None
        self.assertEqual(("public.routes.label",), result.memory_correction.resolution.object_refs)
        self.assertTrue(result.memory_correction.requires_regeneration)
        self.assertEqual(QueryRequestState.CANCELLED, result.memory_correction.query_request_state)
        self.assertEqual("correction-model-1", result.model_id)

        request = provider.requests[0]
        self.assertEqual("intent_correction_interpretation", request["purpose"])
        prompt_input = request["input"]
        assert isinstance(prompt_input, Mapping)
        items = prompt_input["items"]
        assert isinstance(items, Sequence)
        candidate_ids = {
            str(item["id"])
            for item in items
            if isinstance(item, Mapping)
        }
        self.assertIn("public.routes.description", candidate_ids)
        self.assertIn("public.routes.label", candidate_ids)

    def test_ambiguous_free_text_correction_does_not_write_memory(self) -> None:
        query_request = self._query_request()
        provider = IntentCorrectionProvider(
            self._correction_payload(
                corrected_object_ref=None,
                confidence=0.55,
                alternatives=["public.routes.label", "public.routes.code"],
                ambiguities=["Both label and code could mean route name"],
                needs_clarification=True,
            )
        )

        result = self._interpreter(provider).interpret_and_apply(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=query_request.id,
            provider_id="fake",
            correction_text="Per route name intendo il nome usato dagli operatori.",
            correction_classification=Classification.INTERNAL,
            current_entities=self._current_entities(),
            actor_id="steward-1",
        )

        self.assertTrue(result.interpretation.needs_clarification)
        self.assertIsNone(result.memory_correction)
        stored = self.repository.get_query_request(self.tenant.id, query_request.id)
        assert stored is not None
        self.assertEqual(QueryRequestState.READY_FOR_PREVIEW, stored.state)
        self.assertEqual((), self.concepts.list_concepts(self.tenant.id, self.data_source.id))

    def test_low_confidence_resolution_is_converted_to_clarification(self) -> None:
        query_request = self._query_request()
        provider = IntentCorrectionProvider(
            self._correction_payload(
                corrected_object_ref="public.routes.label",
                confidence=0.70,
            )
        )

        result = self._interpreter(provider).interpret_and_apply(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            query_request_id=query_request.id,
            provider_id="fake",
            correction_text="Credo che route name sia label.",
            correction_classification=Classification.INTERNAL,
            current_entities=self._current_entities(),
            actor_id="steward-1",
        )

        self.assertTrue(result.interpretation.needs_clarification)
        self.assertIsNone(result.interpretation.corrected_object_ref)
        self.assertEqual(("public.routes.label",), result.interpretation.alternatives)
        self.assertIsNone(result.memory_correction)

    def test_free_text_correction_rejects_invented_catalog_reference(self) -> None:
        query_request = self._query_request()
        provider = IntentCorrectionProvider(
            self._correction_payload(
                corrected_object_ref="private.unknown.secret_name",
                confidence=0.99,
            )
        )

        with self.assertRaises(InvalidIntentCorrectionOutputError):
            self._interpreter(provider).interpret_and_apply(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                query_request_id=query_request.id,
                provider_id="fake",
                correction_text="Usa secret_name.",
                correction_classification=Classification.INTERNAL,
                current_entities=self._current_entities(),
                actor_id="steward-1",
            )

    def _ingest(self) -> str:
        report = CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.data_source.id,
            DataSourceSnapshot(
                data_source_id=self.data_source.id,
                dialect="postgresql",
                objects=(
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="routes",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "integer", 1, False, is_primary_key=True),
                            ColumnSnapshot("code", "text", 2, False),
                            ColumnSnapshot(
                                "label",
                                "text",
                                3,
                                False,
                                classification=Classification.CONFIDENTIAL,
                            ),
                            ColumnSnapshot("description", "text", 4, True),
                        ),
                    ),
                ),
            ),
        )
        return report.catalog_version_id

    def _query_request(self) -> QueryRequest:
        return self.repository.create_query_request(
            QueryRequest(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                catalog_version_id=self.version_id,
                sql_text="SELECT label FROM public.routes",
                normalized_sql="SELECT label FROM public.routes LIMIT 500",
                referenced_tables=("public.routes",),
                referenced_columns=("public.routes.label",),
                validation_issue_codes=("limit_added",),
                state=QueryRequestState.READY_FOR_PREVIEW,
            )
        )

    def _interpreter(
        self,
        provider: IntentCorrectionProvider,
    ) -> IntentCorrectionInterpreterService:
        gateway = LLMGateway(
            {"fake": provider},
            SchemaQuestionPolicyEngine(
                allowed_provider_ids=frozenset({"fake"}),
                maximum_classification=Classification.CONFIDENTIAL,
            ),
            self.repository,
        )
        return IntentCorrectionInterpreterService(
            self.repository,
            gateway,
            self.memory,
        )

    @staticmethod
    def _current_entities() -> tuple[CurrentIntentEntity, ...]:
        return (
            CurrentIntentEntity(
                term="route name",
                role="selected_column",
                object_ref="public.routes.description",
            ),
        )

    @staticmethod
    def _correction_payload(
        *,
        corrected_object_ref: str | None,
        confidence: float,
        alternatives: list[str] | None = None,
        ambiguities: list[str] | None = None,
        needs_clarification: bool = False,
    ) -> Mapping[str, Any]:
        return {
            "entity_index": 0,
            "term_to_remember": "route name",
            "corrected_object_ref": corrected_object_ref,
            "confidence": confidence,
            "reason": "The user explicitly corrected the selected name column.",
            "alternatives": alternatives or [],
            "ambiguities": ambiguities or [],
            "needs_clarification": needs_clarification,
        }


if __name__ == "__main__":
    unittest.main()
