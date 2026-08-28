from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.cost_engine.sqlverity_cost_engine import FinOpsService
from packages.domain.sqlverity_domain.models import LLMUsageEvent, ModelPricing, TenantBudget


class FinOpsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.service = FinOpsService(self.repository)
        self.now = datetime(2026, 8, 9, 12, tzinfo=UTC)

    def tearDown(self) -> None:
        self.repository.close()

    def test_versioned_pricing_calculates_cached_and_batch_costs(self) -> None:
        pricing = self.repository.create_model_pricing(self._pricing())

        estimate = self.service.estimate(
            tenant_id=self.tenant.id,
            provider_id="provider-a",
            model_id="model-a",
            input_tokens=1_000,
            cached_input_tokens=400,
            output_tokens=200,
            at=self.now,
        )
        batch_estimate = self.service.estimate(
            tenant_id=self.tenant.id,
            provider_id="provider-a",
            model_id="model-a",
            input_tokens=1_000,
            cached_input_tokens=400,
            output_tokens=200,
            at=self.now,
            batch=True,
        )

        assert estimate is not None
        assert batch_estimate is not None
        self.assertEqual(pricing.id, estimate.pricing_id)
        self.assertEqual(Decimal("0.003"), estimate.amount)
        self.assertEqual("0.003", estimate.amount_text)
        self.assertEqual(Decimal("0.00150"), batch_estimate.amount)
        self.assertTrue(batch_estimate.batch_discount_applied)

    def test_overlapping_pricing_and_budgets_are_rejected(self) -> None:
        self.repository.create_model_pricing(self._pricing())
        self.repository.create_tenant_budget(self._budget())

        with self.assertRaisesRegex(ValueError, "pricing validity intervals"):
            self.repository.create_model_pricing(
                self._pricing(valid_from=datetime(2026, 6, 1, tzinfo=UTC))
            )
        with self.assertRaisesRegex(ValueError, "budget validity intervals"):
            self.repository.create_tenant_budget(
                self._budget(valid_from=datetime(2026, 7, 1, tzinfo=UTC))
            )

    def test_budget_authorization_and_monthly_summary_use_recorded_cost(self) -> None:
        pricing = self.repository.create_model_pricing(self._pricing())
        self.repository.create_tenant_budget(self._budget(amount=Decimal("0.010")))
        self.repository.record_llm_usage(
            LLMUsageEvent(
                tenant_id=self.tenant.id,
                provider_id="provider-a",
                model_id="model-a",
                purpose="sql_generation",
                estimated_input_tokens=100,
                estimated_output_tokens=50,
                input_tokens=90,
                cached_input_tokens=30,
                output_tokens=40,
                latency_ms=25,
                estimated_cost="0.008",
                actual_cost="0.007",
                currency="USD",
                pricing_id=pricing.id,
                created_at=self.now,
            )
        )
        self.repository.record_llm_usage(
            LLMUsageEvent(
                tenant_id=self.tenant.id,
                provider_id="provider-b",
                model_id="unpriced",
                purpose="semantic_inference",
                estimated_input_tokens=10,
                estimated_output_tokens=2,
                input_tokens=10,
                output_tokens=2,
                latency_ms=5,
                created_at=self.now,
            )
        )

        estimate = self.service.estimate(
            tenant_id=self.tenant.id,
            provider_id="provider-a",
            model_id="model-a",
            input_tokens=1_000,
            output_tokens=250,
            at=self.now,
        )
        assert estimate is not None
        decision = self.service.authorize(self.tenant.id, estimate, at=self.now)
        summary = self.service.summary(
            tenant_id=self.tenant.id,
            currency="USD",
            at=self.now,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(Decimal("0.011"), decision.projected_amount)
        self.assertEqual(Decimal("0.007"), summary.total_cost)
        self.assertEqual(Decimal("0.003"), summary.remaining_amount)
        self.assertEqual(1, summary.priced_event_count)
        self.assertEqual(1, summary.unpriced_event_count)
        self.assertEqual(1, len(summary.breakdown))
        self.assertEqual(30, summary.breakdown[0].cached_input_tokens)

    def test_validity_timestamps_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self._pricing(valid_from=datetime(2026, 1, 1))

    def _pricing(self, *, valid_from: datetime | None = None) -> ModelPricing:
        return ModelPricing(
            tenant_id=self.tenant.id,
            provider_id="provider-a",
            model_id="model-a",
            valid_from=valid_from or datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2027, 1, 1, tzinfo=UTC),
            currency="USD",
            token_unit=1_000_000,
            input_price_per_unit=Decimal("2"),
            cached_input_price_per_unit=Decimal("0.5"),
            output_price_per_unit=Decimal("8"),
            batch_discount=Decimal("0.5"),
            source_version="provider-price-list-2026-01",
        )

    def _budget(
        self,
        *,
        amount: Decimal = Decimal("10"),
        valid_from: datetime | None = None,
    ) -> TenantBudget:
        return TenantBudget(
            tenant_id=self.tenant.id,
            currency="USD",
            amount=amount,
            valid_from=valid_from or datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2027, 1, 1, tzinfo=UTC),
        )

if __name__ == "__main__":
    unittest.main()
