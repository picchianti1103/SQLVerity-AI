from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.cost_engine.sqlverity_cost_engine import FinOpsService
from packages.domain.sqlverity_domain.budget import (
    LLMBudgetReservation,
    LLMBudgetReservationConflictError,
    LLMBudgetUnavailableError,
    month_bounds,
)
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
    MetadataOnlyPolicyEngine,
    PromptContentItem,
    StructuredLLMRequest,
)
from tests.unit.test_llm_gateway import CapturingProvider


class BudgetReservationCases(unittest.TestCase):
    def repositories(self) -> tuple[SQLiteCatalogRepository, SQLiteCatalogRepository]:
        raise NotImplementedError

    def setUp(self) -> None:
        self.repository, self.other = self.repositories()
        self.addCleanup(self.repository.close)
        self.addCleanup(self.other.close)
        self.repository.initialize()
        self.tenant = self.repository.create_tenant(f"Budget concurrency regression {uuid4()}")
        self.now = datetime.now(UTC)
        self.pricing = self.repository.create_model_pricing(
            ModelPricing(
                tenant_id=self.tenant.id,
                provider_id="fake",
                model_id="fake-model",
                currency="USD",
                token_unit=1,
                input_price_per_unit=Decimal("2"),
                output_price_per_unit=Decimal("6"),
                source_version="synthetic-test",
                valid_from=self.now - timedelta(days=400),
            )
        )
        self.repository.create_tenant_budget(
            TenantBudget(
                tenant_id=self.tenant.id,
                currency="USD",
                amount=Decimal("120"),
                valid_from=self.now - timedelta(days=400),
            )
        )
        self.finops = FinOpsService(self.repository)
        estimate = self.finops.estimate(
            tenant_id=self.tenant.id,
            provider_id="fake",
            model_id="fake-model",
            input_tokens=20,
            output_tokens=10,
            at=self.now,
        )
        assert estimate is not None
        self.estimate = estimate
        self.request = StructuredLLMRequest(
            purpose="semantic_inference",
            instructions="Describe synthetic metadata",
            content=(
                PromptContentItem(
                    id="table",
                    kind="schema_object",
                    classification=Classification.PUBLIC,
                    content={"name": "orders"},
                ),
            ),
            output_schema={"type": "object"},
        )

    def reserve(self, amount: str = "100") -> LLMBudgetReservation:
        return self.finops.reserve(
            self.tenant.id,
            "fake",
            "fake-model",
            replace(self.estimate, amount=Decimal(amount)),
            at=self.now,
        )

    def usage(self, reservation: LLMBudgetReservation, cost: str = "60") -> LLMUsageEvent:
        return LLMUsageEvent(
            tenant_id=self.tenant.id,
            provider_id="fake",
            model_id="fake-model",
            purpose="semantic_inference",
            estimated_input_tokens=20,
            estimated_output_tokens=10,
            input_tokens=18,
            output_tokens=4,
            latency_ms=1,
            actual_cost=cost,
            currency="USD",
            pricing_id=self.pricing.id,
            created_at=reservation.created_at,
        )

    def gateway(
        self, repository: SQLiteCatalogRepository, provider: CapturingProvider
    ) -> LLMGateway:
        return LLMGateway(
            {"fake": provider},
            MetadataOnlyPolicyEngine(),
            repository,
            FinOpsService(repository),
        )

    def test_concurrent_gateway_call_is_denied_before_provider_io(self) -> None:
        started, finish = Event(), Event()
        provider = CapturingProvider(declared_model_id="fake-model")
        other_provider = CapturingProvider(declared_model_id="fake-model")
        original = provider.generate_structured

        def delayed(*args: Any, **kwargs: Any) -> Any:
            started.set()
            if not finish.wait(10):
                raise TimeoutError("Test did not release provider")
            return original(*args, **kwargs)

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch.object(
                provider,
                "generate_structured",
                side_effect=delayed,
            ),
        ):
            pending = pool.submit(
                self.gateway(self.repository, provider).generate_structured,
                tenant_id=self.tenant.id,
                provider_id="fake",
                request=self.request,
            )
            try:
                self.assertTrue(started.wait(10))
                summary = FinOpsService(self.other).summary(
                    currency="USD", tenant_id=self.tenant.id
                )
                self.assertEqual(Decimal("100"), summary.reserved_cost)
                with self.assertRaises(LLMBudgetExceededError):
                    self.gateway(self.other, other_provider).generate_structured(
                        tenant_id=self.tenant.id,
                        provider_id="fake",
                        request=self.request,
                    )
                self.assertEqual([], other_provider.requests)
            finally:
                finish.set()
            result = pending.result(timeout=10)
        self.assertEqual("60", result.usage.actual_cost)
        summary = self.finops.summary(currency="USD", tenant_id=self.tenant.id)
        self.assertEqual(Decimal("60"), summary.total_cost)
        self.assertEqual(0, summary.reserved_cost)
        self.reserve("60")  # The unused reservation is available immediately after settlement.

    def test_two_transactions_cannot_reserve_the_same_remaining_budget(self) -> None:
        barrier = Barrier(2)

        def attempt(repository: SQLiteCatalogRepository) -> bool:
            barrier.wait(timeout=10)
            try:
                FinOpsService(repository).reserve(
                    self.tenant.id,
                    "fake",
                    "fake-model",
                    self.estimate,
                    at=self.now,
                )
                return True
            except LLMBudgetUnavailableError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (self.repository, self.other)))
        self.assertEqual([False, True], sorted(results))

    def test_provider_error_holds_budget_until_audited_reconciliation(self) -> None:
        provider = CapturingProvider(declared_model_id="fake-model", fail=True)
        with self.assertRaises(LLMProviderCallError):
            self.gateway(self.repository, provider).generate_structured(
                tenant_id=self.tenant.id,
                provider_id="fake",
                request=self.request,
            )
        (reservation,) = self.repository.list_llm_budget_reservations(self.tenant.id)
        self.assertEqual("uncertain", reservation.state)
        self.assertEqual(
            100, self.finops.summary(currency="USD", tenant_id=self.tenant.id).uncertain_cost
        )
        with self.assertRaises(LLMBudgetUnavailableError):
            self.reserve()
        self.other.reconcile_llm_budget_reservation(
            self.tenant.id,
            reservation.id,
            actual_cost=Decimal("0"),
            actor_id="operator",
            reason="Provider confirmed request was not billed",
        )
        self.reserve()
        with self.assertRaises(LLMBudgetReservationConflictError):
            self.repository.reconcile_llm_budget_reservation(
                self.tenant.id,
                reservation.id,
                actual_cost=Decimal("0"),
                actor_id="operator",
                reason="Duplicate reconciliation",
            )
        audits = self.repository.audit_events(self.tenant.id)
        resolved = [
            event for event in audits if event.event_type == "finops.reservation_reconciled"
        ]
        self.assertEqual(1, len(resolved))
        self.assertEqual("operator", resolved[0].details["actor_id"])

    def test_failed_pre_dispatch_start_releases_without_provider_call(self) -> None:
        provider = CapturingProvider(declared_model_id="fake-model")
        with patch.object(
            self.repository, "start_llm_budget_reservation", side_effect=RuntimeError
        ):
            with self.assertRaises(LLMProviderCallError):
                self.gateway(self.repository, provider).generate_structured(
                    tenant_id=self.tenant.id,
                    provider_id="fake",
                    request=self.request,
                )
        self.assertEqual([], provider.requests)
        self.assertEqual(
            0, self.finops.summary(currency="USD", tenant_id=self.tenant.id).reserved_cost
        )

    def test_usage_write_failure_rolls_back_settlement_and_keeps_reservation(self) -> None:
        reservation = self.reserve()
        self.finops.start(reservation)
        usage = self.usage(reservation)
        with patch.object(self.repository, "_append_audit", side_effect=RuntimeError("storage")):
            with self.assertRaises(RuntimeError):
                self.finops.settle(reservation, usage)
        self.assertEqual((), self.other.list_llm_usage_events(self.tenant.id))
        self.assertEqual(
            100,
            FinOpsService(self.other)
            .summary(currency="USD", tenant_id=self.tenant.id)
            .reserved_cost,
        )
        self.finops.settle(reservation, usage)
        self.finops.settle(reservation, usage)  # Same event can be retried safely.
        self.assertEqual(1, len(self.repository.list_llm_usage_events(self.tenant.id)))

    def test_manual_charge_and_actual_overrun_remain_visible(self) -> None:
        reservation = self.reserve()
        self.finops.uncertain(reservation)
        self.repository.reconcile_llm_budget_reservation(
            self.tenant.id,
            reservation.id,
            actual_cost=Decimal("150"),
            actor_id="operator",
            reason="Invoice confirms actual charge",
        )
        summary = self.finops.summary(currency="USD", tenant_id=self.tenant.id)
        self.assertEqual(150, summary.total_cost)
        self.assertEqual(150, summary.reconciled_cost)
        self.assertEqual(0, summary.reserved_cost)
        with self.assertRaises(LLMBudgetUnavailableError):
            self.reserve("1")

    def test_late_settlement_stays_in_reserved_month(self) -> None:
        prior = self.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(
            seconds=1
        )
        reservation = self.finops.reserve(
            self.tenant.id,
            "fake",
            "fake-model",
            self.estimate,
            at=prior,
        )
        self.finops.start(reservation)
        self.finops.settle(reservation, self.usage(reservation, "150"))
        self.assertEqual(
            150, self.finops.summary(currency="USD", tenant_id=self.tenant.id, at=prior).total_cost
        )
        self.assertEqual(
            0, self.finops.summary(currency="USD", tenant_id=self.tenant.id).total_cost
        )
        self.reserve()

    def test_live_reservation_cannot_be_manually_released_or_read_by_another_tenant(self) -> None:
        reservation = self.reserve()
        tenant = self.repository.create_tenant(f"Other account {uuid4()}")
        self.assertIsNone(self.other.get_llm_budget_reservation(tenant.id, reservation.id))
        self.assertEqual((), self.other.list_llm_budget_reservations(tenant.id))
        with self.assertRaises(LLMBudgetReservationConflictError):
            self.repository.reconcile_llm_budget_reservation(
                self.tenant.id,
                reservation.id,
                actual_cost=Decimal("0"),
                actor_id="operator",
                reason="Request still running",
            )
        start, end = month_bounds(self.now - timedelta(hours=2))
        abandoned = replace(
            reservation,
            id=tenant.id,
            created_at=self.now - timedelta(hours=2),
            period_start=start,
            period_end=end,
            updated_at=self.now - timedelta(hours=2),
            amount=Decimal("10"),
        )
        # Avoid month-boundary assumptions in the synthetic abandonment fixture.
        self.repository.reserve_llm_budget(replace(abandoned, period_start=start, period_end=end))
        resolved = self.other.reconcile_llm_budget_reservation(
            self.tenant.id,
            abandoned.id,
            actual_cost=Decimal("0"),
            actor_id="operator",
            reason="Worker stopped; provider confirmed no charge",
        )
        self.assertEqual("released", resolved.state)
