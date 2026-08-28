from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.cost_engine.sqlverity_cost_engine import FinOpsService
from packages.domain.sqlverity_domain.contracts import LLMResponse, TokenEstimate
from packages.domain.sqlverity_domain.models import (
    Classification,
    LLMUsageEvent,
    ModelPricing,
    TenantBudget,
)
from packages.llm_gateway.sqlverity_llm_gateway import (
    LLMBudgetExceededError,
    LLMGateway,
    LLMProviderCallError,
    LLMProviderCapabilityError,
    MetadataOnlyPolicyEngine,
    PromptContentItem,
    PromptEgressBlockedError,
    StructuredLLMRequest,
)


class CapturingProvider:
    def __init__(
        self,
        *,
        structured_output: bool = True,
        fail: bool = False,
        declared_model_id: str | None = None,
        cached_input_tokens: int = 0,
    ) -> None:
        self.structured_output = structured_output
        self.fail = fail
        self.declared_model_id = declared_model_id
        self.cached_input_tokens = cached_input_tokens
        self.requests: list[Mapping[str, Any]] = []
        self.closed = False

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return LLMResponse(
            payload={"proposals": []},
            model_id="fake-model",
            input_tokens=18,
            output_tokens=4,
            latency_ms=25,
            cached_input_tokens=self.cached_input_tokens,
        )

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        return TokenEstimate(
            input_tokens=20,
            output_tokens=10,
            cached_input_tokens=5 if self.cached_input_tokens else 0,
            estimated_cost="0.001",
        )

    def capabilities(self) -> Mapping[str, Any]:
        capabilities: dict[str, Any] = {
            "structured_output": self.structured_output,
        }
        if self.declared_model_id is not None:
            capabilities["model_id"] = self.declared_model_id
        return capabilities

    def health_check(self) -> Mapping[str, Any]:
        return {"status": "ok"}

    def close(self) -> None:
        self.closed = True


class CapturingUsageRecorder:
    def __init__(self) -> None:
        self.events: list[LLMUsageEvent] = []

    def record_llm_usage(self, event: LLMUsageEvent) -> None:
        self.events.append(event)


class LLMGatewayTests(unittest.TestCase):
    def test_close_releases_provider_resources(self) -> None:
        provider = CapturingProvider()
        gateway = self._gateway(provider, CapturingUsageRecorder())

        gateway.close()

        self.assertTrue(provider.closed)

    def test_policy_redacts_sensitive_items_before_payload_construction(self) -> None:
        provider = CapturingProvider()
        recorder = CapturingUsageRecorder()
        gateway = self._gateway(provider, recorder)

        result = gateway.generate_structured(
            tenant_id="tenant-1",
            provider_id="fake",
            request=self._request(
                PromptContentItem(
                    id="public.orders.id",
                    kind="schema_column",
                    classification=Classification.INTERNAL,
                    content={"physical_type": "bigint"},
                ),
                PromptContentItem(
                    id="public.orders.email",
                    kind="schema_column",
                    classification=Classification.PII,
                    content={"physical_type": "varchar"},
                ),
            ),
        )

        self.assertEqual(frozenset({"public.orders.id"}), result.included_content_ids)
        self.assertEqual(("public.orders.email",), result.policy_decision.redacted_fields)
        self.assertEqual(1, len(provider.requests))
        prompt_input = provider.requests[0]["input"]
        assert isinstance(prompt_input, Mapping)
        items = prompt_input["items"]
        assert isinstance(items, tuple)
        self.assertEqual("public.orders.id", items[0]["id"])
        self.assertNotIn("email", repr(provider.requests[0]))
        self.assertEqual(1, len(recorder.events))
        self.assertEqual(18, recorder.events[0].input_tokens)
        self.assertEqual("0.001", recorder.events[0].estimated_cost)

    def test_non_metadata_content_is_blocked_before_provider_call(self) -> None:
        provider = CapturingProvider()
        recorder = CapturingUsageRecorder()
        gateway = self._gateway(provider, recorder)

        with self.assertRaises(PromptEgressBlockedError):
            gateway.generate_structured(
                tenant_id="tenant-1",
                provider_id="fake",
                request=self._request(
                    PromptContentItem(
                        id="row-1",
                        kind="raw_row",
                        classification=Classification.INTERNAL,
                        content={"amount": 100},
                    )
                ),
            )

        self.assertEqual([], provider.requests)
        self.assertEqual([], recorder.events)

    def test_preflight_and_structured_required_redaction_are_content_free(self) -> None:
        provider = CapturingProvider(declared_model_id="fake-model")
        gateway = self._gateway(provider, CapturingUsageRecorder())
        request = StructuredLLMRequest(
            purpose="test",
            instructions="Return structured metadata.",
            content=(
                PromptContentItem(
                    id="public.orders.email",
                    kind="schema_column",
                    classification=Classification.PII,
                    content={"description": "Customer email address"},
                ),
            ),
            output_schema={"type": "object"},
            required_content_ids=frozenset({"public.orders.email"}),
            privacy_context={
                "declared_classification": "internal",
                "detected_classification": "pii",
                "effective_classification": "pii",
                "detection_reason_codes": ("email",),
            },
        )

        preflight = gateway.preflight_structured(
            tenant_id="tenant-1",
            provider_id="fake",
            request=request,
        )
        self.assertFalse(preflight.provider_invoked)
        self.assertEqual(frozenset(), preflight.included_content_ids)
        self.assertEqual([], provider.requests)

        with self.assertRaises(PromptEgressBlockedError) as raised:
            gateway.generate_structured(
                tenant_id="tenant-1",
                provider_id="fake",
                request=request,
            )
        detail = dict(raised.exception.safe_detail())
        self.assertEqual("required_prompt_content_redacted", detail["code"])
        self.assertFalse(detail["provider_invoked"])
        self.assertEqual("pii", detail["effective_classification"])
        self.assertEqual(("email",), detail["detection_reason_codes"])
        self.assertEqual(
            (
                {
                    "id": "public.orders.email",
                    "kind": "schema_column",
                },
            ),
            detail["redacted_required_items"],
        )
        self.assertNotIn("Customer email address", repr(detail))
        self.assertEqual([], provider.requests)

    def test_provider_must_guarantee_structured_output(self) -> None:
        provider = CapturingProvider(structured_output=False)
        gateway = self._gateway(provider, CapturingUsageRecorder())
        request = self._request(self._internal_table())

        with self.assertRaises(LLMProviderCapabilityError):
            gateway.preflight_structured(
                tenant_id="tenant-1",
                provider_id="fake",
                request=request,
            )
        with self.assertRaises(LLMProviderCapabilityError):
            gateway.generate_structured(
                tenant_id="tenant-1",
                provider_id="fake",
                request=request,
            )
        self.assertEqual([], provider.requests)

    def test_provider_failure_is_normalized(self) -> None:
        provider = CapturingProvider(fail=True)
        gateway = self._gateway(provider, CapturingUsageRecorder())

        with self.assertRaises(LLMProviderCallError):
            gateway.generate_structured(
                tenant_id="tenant-1",
                provider_id="fake",
                request=self._request(self._internal_table()),
            )

    def test_request_rejects_duplicate_content_identifiers(self) -> None:
        item = self._internal_table()
        with self.assertRaises(ValueError):
            self._request(item, item)

    def test_budget_blocks_before_provider_call(self) -> None:
        repository = SQLiteCatalogRepository()
        repository.initialize()
        try:
            tenant = repository.create_tenant("FinOps tenant")
            repository.create_model_pricing(self._pricing(tenant.id))
            repository.create_tenant_budget(
                TenantBudget(
                    tenant_id=tenant.id,
                    currency="USD",
                    amount=Decimal("0.00001"),
                    valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                    valid_to=datetime(2027, 1, 1, tzinfo=UTC),
                )
            )
            provider = CapturingProvider(declared_model_id="fake-model")
            gateway = LLMGateway(
                {"fake": provider},
                MetadataOnlyPolicyEngine(allowed_provider_ids=frozenset({"fake"})),
                repository,
                FinOpsService(repository),
            )

            with self.assertRaises(LLMBudgetExceededError):
                gateway.generate_structured(
                    tenant_id=tenant.id,
                    provider_id="fake",
                    request=self._request(self._internal_table()),
                )

            self.assertEqual([], provider.requests)
            self.assertEqual((), repository.list_llm_usage_events(tenant.id))

            undeclared_provider = CapturingProvider()
            undeclared_gateway = LLMGateway(
                {"fake": undeclared_provider},
                MetadataOnlyPolicyEngine(allowed_provider_ids=frozenset({"fake"})),
                repository,
                FinOpsService(repository),
            )
            with self.assertRaisesRegex(LLMBudgetExceededError, "declare its model id"):
                undeclared_gateway.generate_structured(
                    tenant_id=tenant.id,
                    provider_id="fake",
                    request=self._request(self._internal_table()),
                )
            self.assertEqual([], undeclared_provider.requests)

            unpriced_provider = CapturingProvider(declared_model_id="unpriced-model")
            unpriced_gateway = LLMGateway(
                {"fake": unpriced_provider},
                MetadataOnlyPolicyEngine(allowed_provider_ids=frozenset({"fake"})),
                repository,
                FinOpsService(repository),
            )
            with self.assertRaisesRegex(LLMBudgetExceededError, "applicable model pricing"):
                unpriced_gateway.generate_structured(
                    tenant_id=tenant.id,
                    provider_id="fake",
                    request=self._request(self._internal_table()),
                )
            self.assertEqual([], unpriced_provider.requests)
        finally:
            repository.close()

    def test_gateway_records_priced_estimated_and_actual_cost(self) -> None:
        repository = SQLiteCatalogRepository()
        repository.initialize()
        try:
            tenant = repository.create_tenant("Priced tenant")
            pricing = repository.create_model_pricing(self._pricing(tenant.id))
            provider = CapturingProvider(
                declared_model_id="fake-model",
                cached_input_tokens=3,
            )
            gateway = LLMGateway(
                {"fake": provider},
                MetadataOnlyPolicyEngine(allowed_provider_ids=frozenset({"fake"})),
                repository,
                FinOpsService(repository),
            )

            result = gateway.generate_structured(
                tenant_id=tenant.id,
                provider_id="fake",
                request=self._request(self._internal_table()),
            )

            self.assertEqual("0.0001125", result.usage.estimated_cost)
            self.assertEqual("0.0000635", result.usage.actual_cost)
            self.assertEqual("USD", result.usage.currency)
            self.assertEqual(pricing.id, result.usage.pricing_id)
            self.assertEqual(3, result.usage.cached_input_tokens)
        finally:
            repository.close()

    @staticmethod
    def _gateway(
        provider: CapturingProvider,
        recorder: CapturingUsageRecorder,
    ) -> LLMGateway:
        return LLMGateway(
            {"fake": provider},
            MetadataOnlyPolicyEngine(allowed_provider_ids=frozenset({"fake"})),
            recorder,
        )

    @staticmethod
    def _request(*items: PromptContentItem) -> StructuredLLMRequest:
        return StructuredLLMRequest(
            purpose="test",
            instructions="Return structured metadata.",
            content=items,
            output_schema={"type": "object"},
        )

    @staticmethod
    def _internal_table() -> PromptContentItem:
        return PromptContentItem(
            id="public.orders",
            kind="schema_object",
            classification=Classification.INTERNAL,
            content={"object_kind": "table"},
        )

    @staticmethod
    def _pricing(tenant_id: str) -> ModelPricing:
        return ModelPricing(
            tenant_id=tenant_id,
            provider_id="fake",
            model_id="fake-model",
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2027, 1, 1, tzinfo=UTC),
            currency="USD",
            token_unit=1_000_000,
            input_price_per_unit=Decimal("2"),
            cached_input_price_per_unit=Decimal("0.5"),
            output_price_per_unit=Decimal("8"),
            source_version="test-2026",
        )


if __name__ == "__main__":
    unittest.main()
