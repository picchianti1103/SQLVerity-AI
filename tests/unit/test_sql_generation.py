from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from packages.catalog.sqlverity_catalog.analytics_semantics import AnalyticsSemanticsService
from packages.catalog.sqlverity_catalog.business_concepts import BusinessConceptService
from packages.catalog.sqlverity_catalog.ingestion import CatalogIngestionService
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    LLMResponse,
    RelationshipSnapshot,
    SchemaObjectSnapshot,
    TokenEstimate,
)
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceType,
    EpistemicStatus,
    ObjectKind,
    QueryRequestState,
)
from packages.learning.sqlverity_learning import LearningLoopService
from packages.llm_gateway.sqlverity_llm_gateway import (
    LLMGateway,
    PromptEgressBlockedError,
    SchemaQuestionPolicyEngine,
)
from packages.query.sqlverity_query import (
    InvalidSQLProposalOutputError,
    SQLGenerationService,
)
from packages.retrieval.sqlverity_retrieval import ContextBuilderService, ContextNoMatchesError
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator


class SQLProposalProvider:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.requests: list[Mapping[str, Any]] = []

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            payload=self.payload,
            model_id="sql-model-1",
            input_tokens=160,
            output_tokens=45,
            latency_ms=95,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(input_tokens=170, output_tokens=60, estimated_cost="0.006")

    def capabilities(self) -> Mapping[str, Any]:
        return {"structured_output": True}

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}


class SequencedSQLProposalProvider(SQLProposalProvider):
    def __init__(self, payloads: Sequence[Mapping[str, Any]]) -> None:
        if not payloads:
            raise ValueError("At least one payload is required")
        super().__init__(payloads[-1])
        self.payloads = tuple(payloads)

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        payload_index = min(len(self.requests), len(self.payloads) - 1)
        self.requests.append(request)
        return LLMResponse(
            payload=self.payloads[payload_index],
            model_id="sql-model-1",
            input_tokens=160,
            output_tokens=45,
            latency_ms=95,
        )


class SQLGenerationTests(unittest.TestCase):
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
        CatalogIngestionService(self.repository, {}).ingest_snapshot(
            self.tenant.id,
            self.data_source.id,
            DataSourceSnapshot(
                data_source_id=self.data_source.id,
                dialect="postgresql",
                objects=(
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="customers",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "bigint", 1, False, is_primary_key=True),
                            ColumnSnapshot(
                                "email",
                                "varchar(255)",
                                2,
                                False,
                                classification=Classification.PII,
                            ),
                        ),
                    ),
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="orders",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "bigint", 1, False, is_primary_key=True),
                            ColumnSnapshot("customer_id", "bigint", 2, False),
                        ),
                    ),
                    SchemaObjectSnapshot(
                        schema_name="public",
                        name="audit_log",
                        kind=ObjectKind.TABLE,
                        columns=(ColumnSnapshot("id", "bigint", 1, False),),
                    ),
                    SchemaObjectSnapshot(
                        schema_name="network",
                        name="routes",
                        kind=ObjectKind.TABLE,
                        columns=(
                            ColumnSnapshot("id", "integer", 1, False, is_primary_key=True),
                            ColumnSnapshot("code", "text", 2, False),
                            ColumnSnapshot("display_name", "text", 3, False),
                            ColumnSnapshot("description", "text", 4, True),
                        ),
                    ),
                ),
                relationships=(
                    RelationshipSnapshot(
                        name="orders_customer_fkey",
                        source_object_ref="public.orders",
                        target_object_ref="public.customers",
                        source_columns=("customer_id",),
                        target_columns=("id",),
                    ),
                ),
            ),
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_generation_uses_retrieved_and_policy_filtered_context(self) -> None:
        provider = SQLProposalProvider(
            self._payload(
                sql="SELECT id FROM public.orders",
                tables=["public.orders"],
                columns=["public.orders.id"],
            )
        )

        result = self._service(provider).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=2,
        )

        self.assertEqual("accepted", result.validation_status)
        self.assertEqual(QueryRequestState.READY_FOR_PREVIEW, result.state)
        stored = self.repository.get_query_request(self.tenant.id, result.request_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual("fake", stored.provider_id)
        self.assertEqual("sql-model-1", stored.model_id)
        self.assertEqual(result.usage.id, stored.llm_usage_event_id)
        self.assertTrue(result.ready_for_preview)
        self.assertFalse(result.ready_for_execution)
        self.assertTrue(result.validation.accepted)
        self.assertEqual(
            "SELECT id FROM public.orders LIMIT 500",
            result.validation.normalized_sql,
        )
        self.assertEqual(("public.orders",), result.proposal.tables)
        self.assertIn("public.customers.email", result.redacted_content_ids)
        requested_ids = self._requested_ids(provider.requests[0])
        self.assertIn("__request.question", requested_ids)
        self.assertIn("public.orders", requested_ids)
        self.assertIn("public.customers", requested_ids)
        self.assertNotIn("public.customers.email", requested_ids)
        self.assertNotIn("public.audit_log", requested_ids)
        self.assertEqual(1, len(self.repository.list_llm_usage_events(self.tenant.id)))

    def test_maximum_privacy_does_not_retry_invalid_intent_mapping(self) -> None:
        provider = SequencedSQLProposalProvider(
            (
                self._payload(
                    interpretation={
                        "kind": "record_list",
                        "summary": "Elenca gli ordini.",
                        "requested_row_limit": None,
                        "entities": [
                            {
                                "term": "orders",
                                "object_ref": "orders",
                                "role": "primary_table",
                                "confidence": 0.8,
                                "reason": "Riferimento non qualificato.",
                                "alternatives": [],
                            }
                        ],
                    }
                ),
                self._payload(),
            )
        )

        with self.assertRaises(InvalidSQLProposalOutputError):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="Show orders",
                question_classification=Classification.INTERNAL,
                max_seed_objects=1,
                max_objects=1,
                graph_hops=0,
                privacy_mode="maximum_privacy",
            )

        self.assertEqual(1, len(provider.requests))
        self.assertEqual(1, len(self.repository.list_llm_usage_events(self.tenant.id)))

    def test_governed_semantic_mode_retries_invalid_intent_mapping_once(self) -> None:
        provider = SequencedSQLProposalProvider(
            (
                self._payload(
                    interpretation={
                        "kind": "record_list",
                        "summary": "Elenca gli ordini.",
                        "requested_row_limit": None,
                        "entities": [
                            {
                                "term": "orders",
                                "object_ref": "orders",
                                "role": "primary_table",
                                "confidence": 0.8,
                                "reason": "Riferimento non qualificato.",
                                "alternatives": [],
                            }
                        ],
                    }
                ),
                self._payload(),
            )
        )

        result = self._service(provider).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
            privacy_mode="governed_semantic",
        )

        self.assertEqual("governed_semantic", result.privacy_mode)
        self.assertEqual("semantic_fallback", result.generation_strategy)
        self.assertEqual(2, result.generation_attempt_count)
        self.assertEqual(
            "deterministic_intent_mapping_invalid",
            result.fallback_reason,
        )
        self.assertTrue(result.validation.accepted)
        self.assertEqual(2, len(provider.requests))
        self.assertIn(
            "synonyms, inflections, temporal language",
            provider.requests[1]["instructions"],
        )
        self.assertEqual(2, len(self.repository.list_llm_usage_events(self.tenant.id)))

    def test_explicit_semantic_retry_requires_privacy_opt_in(self) -> None:
        provider = SQLProposalProvider(self._payload())

        with self.assertRaisesRegex(ValueError, "governed_semantic"):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="Show orders",
                question_classification=Classification.INTERNAL,
                force_semantic=True,
            )

        self.assertEqual([], provider.requests)

    def test_explicit_semantic_retry_is_reported_as_user_requested(self) -> None:
        provider = SQLProposalProvider(self._payload())

        result = self._service(provider).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
            privacy_mode="governed_semantic",
            force_semantic=True,
        )

        self.assertEqual("semantic_user_retry", result.generation_strategy)
        self.assertEqual(1, result.generation_attempt_count)
        self.assertIsNone(result.fallback_reason)
        self.assertIn("copied exactly", provider.requests[0]["instructions"])

    def test_table_preview_interpretation_resolves_schema_and_italian_row_limit(self) -> None:
        interpretation = {
            "kind": "table_preview",
            "summary": "Mostra le prime dieci righe della tabella delle rotte.",
            "requested_row_limit": 10,
            "entities": [
                {
                    "term": "routes",
                    "object_ref": "network.routes",
                    "role": "primary_table",
                    "confidence": 1.0,
                    "reason": "Il nome fisico della tabella coincide con il termine routes.",
                    "alternatives": [],
                }
            ],
        }
        provider = SQLProposalProvider(
            self._payload(
                sql=(
                    "SELECT id, code, display_name, description "
                    "FROM network.routes LIMIT 10"
                ),
                tables=["network.routes"],
                columns=[
                    "network.routes.id",
                    "network.routes.code",
                    "network.routes.display_name",
                    "network.routes.description",
                ],
                interpretation=interpretation,
            )
        )

        result = self._service(provider).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Mostrami le prime dieci righe della tabella routes",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        self.assertEqual("table_preview", result.interpretation.kind)
        self.assertEqual(10, result.interpretation.requested_row_limit)
        self.assertEqual("network.routes", result.interpretation.entities[0].object_ref)
        self.assertEqual(
            "SELECT id, code, display_name, description FROM network.routes LIMIT 10",
            result.validation.normalized_sql,
        )
        request_input = provider.requests[0]["input"]
        assert isinstance(request_input, Mapping)
        request_items = request_input["items"]
        assert isinstance(request_items, Sequence)
        target = next(
            item
            for item in request_items
            if isinstance(item, Mapping) and item.get("id") == "__request.target"
        )
        target_content = target["data"]
        assert isinstance(target_content, Mapping)
        intent_hints = target_content["intent_hints"]
        assert isinstance(intent_hints, Mapping)
        self.assertEqual(10, intent_hints["requested_row_limit"])
        self.assertEqual("table_preview", intent_hints["suggested_kind"])

    def test_interpretation_cannot_change_explicit_row_limit(self) -> None:
        provider = SQLProposalProvider(
            self._payload(
                interpretation={
                    "kind": "table_preview",
                    "summary": "Mostra venti righe.",
                    "requested_row_limit": 20,
                    "entities": [
                        {
                            "term": "orders",
                            "object_ref": "public.orders",
                            "role": "primary_table",
                            "confidence": 1.0,
                            "reason": "Corrispondenza esatta.",
                            "alternatives": [],
                        }
                    ],
                }
            )
        )

        with self.assertRaises(InvalidSQLProposalOutputError):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="Show the first 10 rows of orders",
                question_classification=Classification.INTERNAL,
                max_seed_objects=1,
                max_objects=1,
                graph_hops=0,
            )

    def test_generated_sql_must_match_interpreted_row_limit(self) -> None:
        provider = SQLProposalProvider(
            self._payload(
                sql="SELECT id FROM public.orders LIMIT 5",
                interpretation={
                    "kind": "table_preview",
                    "summary": "Mostra dieci righe degli ordini.",
                    "requested_row_limit": 10,
                    "entities": [
                        {
                            "term": "orders",
                            "object_ref": "public.orders",
                            "role": "primary_table",
                            "confidence": 1.0,
                            "reason": "Corrispondenza esatta.",
                            "alternatives": [],
                        }
                    ],
                },
            )
        )

        with self.assertRaises(InvalidSQLProposalOutputError):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="Show the first 10 rows of orders",
                question_classification=Classification.INTERNAL,
                max_seed_objects=1,
                max_objects=1,
                graph_hops=0,
            )

    def test_sensitive_question_is_blocked_before_provider_call(self) -> None:
        provider = SQLProposalProvider(self._payload())

        with self.assertRaises(PromptEgressBlockedError):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="Show orders",
                question_classification=Classification.PII,
            )

        self.assertEqual([], provider.requests)
        self.assertEqual((), self.repository.list_llm_usage_events(self.tenant.id))

    def test_unsafe_sql_is_returned_as_rejected_without_preview_sql(self) -> None:
        provider = SQLProposalProvider(
            self._payload(
                sql="SELECT id FROM public.orders FOR UPDATE",
                tables=["public.orders"],
                columns=["public.orders.id"],
            )
        )

        result = self._service(provider).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=2,
        )

        self.assertEqual("rejected", result.validation_status)
        self.assertEqual(QueryRequestState.REJECTED, result.state)
        self.assertFalse(result.ready_for_preview)
        self.assertFalse(result.ready_for_execution)
        self.assertIsNone(result.validation.normalized_sql)
        self.assertIn(
            "locking_clause_not_allowed",
            {issue.code for issue in result.validation.issues},
        )

    def test_generated_named_parameters_are_persisted_without_values(self) -> None:
        provider = SQLProposalProvider(
            self._payload(
                sql="SELECT id FROM public.orders WHERE id = :order_id",
                parameters=[
                    {"name": "order_id", "type": "integer", "nullable": False}
                ],
            )
        )

        result = self._service(provider).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Show orders with id 42",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        self.assertTrue(result.ready_for_preview)
        self.assertEqual("order_id", result.proposal.parameters[0].name)
        stored = self.repository.get_query_request(self.tenant.id, result.request_id)
        assert stored is not None
        self.assertEqual(("order_id",), stored.parameter_names)
        self.assertIsNone(stored.parameter_value_hash)
        self.assertEqual("integer", stored.parameter_definitions[0].value_type.value)

    def test_output_cannot_reintroduce_a_redacted_column(self) -> None:
        provider = SQLProposalProvider(
            self._payload(
                sql="SELECT email FROM public.customers",
                tables=["public.customers"],
                columns=["public.customers.email"],
            )
        )

        with self.assertRaises(InvalidSQLProposalOutputError):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="Show orders",
                question_classification=Classification.INTERNAL,
                max_seed_objects=1,
                max_objects=2,
            )

        self.assertEqual(1, len(self.repository.list_llm_usage_events(self.tenant.id)))

    def test_no_context_match_stops_before_provider_call(self) -> None:
        provider = SQLProposalProvider(self._payload())

        with self.assertRaises(ContextNoMatchesError):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="weather observations",
                question_classification=Classification.INTERNAL,
            )

        self.assertEqual([], provider.requests)

    def test_sensitive_corrected_example_is_retrieved_but_redacted_from_prompt(self) -> None:
        learning_loop = LearningLoopService(
            self.repository,
            PostgreSQLSQLValidator(),
        )
        example = learning_loop.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            question="Show orders",
            corrected_sql="SELECT id FROM public.orders",
            actor_id="steward-1",
            content_classification=Classification.PII,
        )
        provider = SQLProposalProvider(self._payload())

        result = self._service(provider, learning_loop).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
        )

        content_id = f"__sql_example.{example.example.id}"
        self.assertEqual(result.context.sql_examples[0].id, example.example.id)
        self.assertIn(content_id, result.redacted_content_ids)
        self.assertNotIn(content_id, self._requested_ids(provider.requests[0]))

    def test_internal_corrected_example_is_included_in_prompt(self) -> None:
        learning_loop = LearningLoopService(
            self.repository,
            PostgreSQLSQLValidator(),
        )
        example = learning_loop.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            question="Show orders",
            corrected_sql="SELECT id FROM public.orders",
            actor_id="steward-1",
            content_classification=Classification.INTERNAL,
        )
        provider = SQLProposalProvider(self._payload())

        self._service(provider, learning_loop).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Show orders",
            question_classification=Classification.INTERNAL,
        )

        content_id = f"__sql_example.{example.example.id}"
        self.assertIn(content_id, self._requested_ids(provider.requests[0]))

    def test_governed_business_concept_drives_context_and_provenance(self) -> None:
        concepts = BusinessConceptService(self.repository)
        concepts.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            concept_key="gross_revenue",
            name="Gross revenue",
            description="Gross booked order value",
            synonyms=("Fatturato",),
            object_refs=("public.orders.id",),
            content_classification=Classification.INTERNAL,
            actor_id="finance-steward",
        )
        provider = SQLProposalProvider(
            self._payload(business_concepts=["gross_revenue"])
        )

        result = self._service(provider, concepts=concepts).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Mostra il fatturato",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        self.assertEqual(("gross_revenue",), result.proposal.business_concepts)
        self.assertEqual(EpistemicStatus.CONFIRMED, result.context.business_concepts[0].status)
        self.assertIn(
            "__business_concept.gross_revenue",
            self._requested_ids(provider.requests[0]),
        )

    def test_output_cannot_invent_a_business_concept(self) -> None:
        provider = SQLProposalProvider(
            self._payload(business_concepts=["invented_metric"])
        )

        with self.assertRaises(InvalidSQLProposalOutputError):
            self._service(provider).generate(
                tenant_id=self.tenant.id,
                data_source_id=self.data_source.id,
                provider_id="fake",
                question="Show orders",
                question_classification=Classification.INTERNAL,
            )

    def test_sensitive_business_concept_seeds_locally_but_is_redacted(self) -> None:
        concepts = BusinessConceptService(self.repository)
        concepts.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            concept_key="gross_revenue",
            name="Gross revenue",
            description="Sensitive commercial definition",
            synonyms=("Fatturato",),
            object_refs=("public.orders.id",),
            content_classification=Classification.CONFIDENTIAL,
            actor_id="finance-steward",
        )
        provider = SQLProposalProvider(self._payload())

        result = self._service(provider, concepts=concepts).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Mostra il fatturato",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        content_id = "__business_concept.gross_revenue"
        self.assertEqual("public.orders", result.context.objects[0].reference)
        self.assertIn(content_id, result.redacted_content_ids)
        self.assertNotIn(content_id, self._requested_ids(provider.requests[0]))

    def test_governed_metric_and_rule_enter_prompt_and_query_provenance(self) -> None:
        concepts = BusinessConceptService(self.repository)
        concepts.correct(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            concept_key="order_volume",
            name="Order volume",
            description="Number of valid orders",
            synonyms=("Numero ordini",),
            object_refs=("public.orders.id",),
            content_classification=Classification.INTERNAL,
            actor_id="steward",
        )
        semantics = AnalyticsSemanticsService(self.repository, concepts)
        semantics.correct_business_rule(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            rule_key="valid_order",
            name="Valid order",
            description="Order id must be positive",
            predicate_sql="public.orders.id > 0",
            concept_keys=("order_volume",),
            content_classification=Classification.INTERNAL,
            actor_id="steward",
        )
        semantics.correct_metric(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            metric_key="order_count",
            name="Order count",
            description="Count of valid orders",
            expression_sql="COUNT(public.orders.id)",
            grain_refs=("public.orders.id",),
            dimension_refs=(),
            concept_keys=("order_volume",),
            rule_keys=("valid_order",),
            content_classification=Classification.INTERNAL,
            actor_id="steward",
        )
        provider = SQLProposalProvider(
            self._payload(
                sql=(
                    "SELECT COUNT(public.orders.id) FROM public.orders "
                    "WHERE public.orders.id > 0"
                ),
                tables=["public.orders"],
                columns=["public.orders.id"],
                business_concepts=["order_volume"],
                metrics=["order_count"],
                business_rules=["valid_order"],
            )
        )

        result = self._service(
            provider,
            concepts=concepts,
            analytics_semantics=semantics,
        ).generate(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            provider_id="fake",
            question="Mostra il numero ordini",
            question_classification=Classification.INTERNAL,
            max_seed_objects=1,
            max_objects=1,
            graph_hops=0,
        )

        requested = self._requested_ids(provider.requests[0])
        self.assertIn("__metric.order_count", requested)
        self.assertIn("__business_rule.valid_order", requested)
        self.assertEqual(("order_count",), result.proposal.metrics)
        self.assertEqual(("valid_order",), result.proposal.business_rules)
        stored = self.repository.get_query_request(self.tenant.id, result.request_id)
        assert stored is not None
        self.assertEqual(("order_count",), stored.metrics)
        self.assertEqual(("valid_order",), stored.business_rules)

        invalid_sql = (
            "SELECT SUM(public.orders.id) FROM public.orders "
            "WHERE public.orders.id > 0"
        )
        missing_rule_sql = "SELECT COUNT(public.orders.id) FROM public.orders"
        for sql in (invalid_sql, missing_rule_sql):
            invalid_provider = SQLProposalProvider(
                self._payload(
                    sql=sql,
                    tables=["public.orders"],
                    columns=["public.orders.id"],
                    business_concepts=["order_volume"],
                    metrics=["order_count"],
                    business_rules=["valid_order"],
                )
            )
            with self.subTest(sql=sql), self.assertRaises(
                InvalidSQLProposalOutputError
            ):
                self._service(
                    invalid_provider,
                    concepts=concepts,
                    analytics_semantics=semantics,
                ).generate(
                    tenant_id=self.tenant.id,
                    data_source_id=self.data_source.id,
                    provider_id="fake",
                    question="Mostra il numero ordini",
                    question_classification=Classification.INTERNAL,
                    max_seed_objects=1,
                    max_objects=1,
                    graph_hops=0,
                )

    def _service(
        self,
        provider: SQLProposalProvider,
        learning_loop: LearningLoopService | None = None,
        concepts: BusinessConceptService | None = None,
        analytics_semantics: AnalyticsSemanticsService | None = None,
    ) -> SQLGenerationService:
        gateway = LLMGateway(
            {"fake": provider},
            SchemaQuestionPolicyEngine(
                allowed_provider_ids=frozenset({"fake"})
            ),
            self.repository,
        )
        return SQLGenerationService(
            ContextBuilderService(
                self.repository,
                learning_loop,
                concepts,
                analytics_semantics,
            ),
            gateway,
            PostgreSQLSQLValidator(),
            self.repository,
        )

    @staticmethod
    def _payload(
        *,
        sql: str = "SELECT id FROM public.orders",
        tables: list[str] | None = None,
        columns: list[str] | None = None,
        business_concepts: list[str] | None = None,
        metrics: list[str] | None = None,
        business_rules: list[str] | None = None,
        parameters: list[Mapping[str, Any]] | None = None,
        interpretation: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        selected_tables = tables if tables is not None else ["public.orders"]
        selected_columns = columns if columns is not None else ["public.orders.id"]
        return {
            "intent": "data_query",
            "interpretation": interpretation
            or {
                "kind": "record_list",
                "summary": "Elenca gli ordini.",
                "requested_row_limit": None,
                "entities": [
                    {
                        "term": "orders",
                        "object_ref": selected_tables[0],
                        "role": "primary_table",
                        "confidence": 1.0,
                        "reason": "Il nome della tabella coincide con il termine richiesto.",
                        "alternatives": [],
                    }
                ],
            },
            "sql": sql,
            "dialect": "postgresql",
            "tables": selected_tables,
            "columns": selected_columns,
            "business_concepts": business_concepts or [],
            "metrics": metrics or [],
            "business_rules": business_rules or [],
            "assumptions": [],
            "parameters": parameters or [],
            "ambiguities": [],
            "needs_clarification": False,
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
