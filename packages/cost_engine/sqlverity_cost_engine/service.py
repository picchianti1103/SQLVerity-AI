from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from packages.domain.sqlverity_domain.models import LLMUsageEvent, ModelPricing, TenantBudget


class FinOpsRepository(Protocol):
    def get_effective_model_pricing(
        self,
        tenant_id: str,
        provider_id: str,
        model_id: str,
        at: datetime,
    ) -> ModelPricing | None: ...

    def get_effective_tenant_budget(
        self,
        tenant_id: str,
        currency: str,
        at: datetime,
    ) -> TenantBudget | None: ...

    def list_tenant_budgets(self, tenant_id: str) -> tuple[TenantBudget, ...]: ...

    def list_llm_usage_events(self, tenant_id: str) -> tuple[LLMUsageEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class CostEstimate:
    pricing_id: str
    currency: str
    amount: Decimal
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    batch_discount_applied: bool

    @property
    def amount_text(self) -> str:
        return _decimal_text(self.amount)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    currency: str
    budget_id: str | None
    budget_amount: Decimal | None
    spent_amount: Decimal
    projected_amount: Decimal
    remaining_amount: Decimal | None
    reason: str


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    provider_id: str
    model_id: str
    purpose: str
    event_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost: Decimal


@dataclass(frozen=True, slots=True)
class FinOpsSummary:
    tenant_id: str
    currency: str
    period_start: datetime
    period_end: datetime
    total_cost: Decimal
    budget_id: str | None
    budget_amount: Decimal | None
    remaining_amount: Decimal | None
    priced_event_count: int
    unpriced_event_count: int
    breakdown: tuple[UsageBreakdown, ...]


class FinOpsService:
    def __init__(self, repository: FinOpsRepository) -> None:
        self._repository = repository

    def estimate(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        at: datetime | None = None,
        batch: bool = False,
    ) -> CostEstimate | None:
        if input_tokens < 0 or output_tokens < 0 or cached_input_tokens < 0:
            raise ValueError("Token counts must not be negative")
        if cached_input_tokens > input_tokens:
            raise ValueError("Cached input tokens cannot exceed input tokens")
        effective_at = at or datetime.now(UTC)
        pricing = self._repository.get_effective_model_pricing(
            tenant_id,
            provider_id,
            model_id,
            effective_at,
        )
        if pricing is None:
            return None
        cached_price = (
            pricing.cached_input_price_per_unit
            if pricing.cached_input_price_per_unit is not None
            else pricing.input_price_per_unit
        )
        uncached_input_tokens = input_tokens - cached_input_tokens
        raw_amount = (
            Decimal(uncached_input_tokens) * pricing.input_price_per_unit
            + Decimal(cached_input_tokens) * cached_price
            + Decimal(output_tokens) * pricing.output_price_per_unit
        ) / Decimal(pricing.token_unit)
        if batch:
            raw_amount *= Decimal("1") - pricing.batch_discount
        return CostEstimate(
            pricing_id=pricing.id,
            currency=pricing.currency,
            amount=raw_amount,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            batch_discount_applied=batch and pricing.batch_discount > 0,
        )

    def authorize(
        self,
        tenant_id: str,
        estimate: CostEstimate,
        *,
        at: datetime | None = None,
    ) -> BudgetDecision:
        effective_at = at or datetime.now(UTC)
        budget = self._repository.get_effective_tenant_budget(
            tenant_id,
            estimate.currency,
            effective_at,
        )
        period_start, period_end = _month_bounds(effective_at)
        spent = self._spent(
            tenant_id,
            estimate.currency,
            period_start,
            period_end,
        )
        projected = spent + estimate.amount
        if budget is None:
            return BudgetDecision(
                allowed=True,
                currency=estimate.currency,
                budget_id=None,
                budget_amount=None,
                spent_amount=spent,
                projected_amount=projected,
                remaining_amount=None,
                reason="No active tenant budget is configured",
            )
        remaining = budget.amount - projected
        return BudgetDecision(
            allowed=projected <= budget.amount,
            currency=estimate.currency,
            budget_id=budget.id,
            budget_amount=budget.amount,
            spent_amount=spent,
            projected_amount=projected,
            remaining_amount=max(Decimal("0"), remaining),
            reason=(
                "Projected monthly cost is within budget"
                if projected <= budget.amount
                else "Projected monthly cost exceeds budget"
            ),
        )

    def has_active_budget(
        self,
        tenant_id: str,
        *,
        currency: str | None = None,
        at: datetime | None = None,
    ) -> bool:
        effective_at = at or datetime.now(UTC)
        if effective_at.tzinfo is None:
            raise ValueError("FinOps timestamps must be timezone-aware")
        return any(
            (currency is None or budget.currency == currency)
            and budget.valid_from <= effective_at
            and (budget.valid_to is None or effective_at < budget.valid_to)
            for budget in self._repository.list_tenant_budgets(tenant_id)
        )

    def summary(
        self,
        *,
        tenant_id: str,
        currency: str,
        at: datetime | None = None,
    ) -> FinOpsSummary:
        effective_at = at or datetime.now(UTC)
        period_start, period_end = _month_bounds(effective_at)
        budget = self._repository.get_effective_tenant_budget(
            tenant_id,
            currency,
            effective_at,
        )
        aggregates: dict[tuple[str, str, str], list[int | Decimal]] = defaultdict(
            lambda: [0, 0, 0, 0, Decimal("0")]
        )
        priced_event_count = 0
        unpriced_event_count = 0
        for event in self._repository.list_llm_usage_events(tenant_id):
            if not period_start <= event.created_at < period_end:
                continue
            cost = _event_cost(event, currency)
            if cost is None:
                if event.currency is None or event.currency == currency:
                    unpriced_event_count += 1
                continue
            priced_event_count += 1
            values = aggregates[(event.provider_id, event.model_id, event.purpose)]
            values[0] = int(values[0]) + 1
            values[1] = int(values[1]) + event.input_tokens
            values[2] = int(values[2]) + event.cached_input_tokens
            values[3] = int(values[3]) + event.output_tokens
            values[4] = Decimal(values[4]) + cost
        breakdown = tuple(
            UsageBreakdown(
                provider_id=key[0],
                model_id=key[1],
                purpose=key[2],
                event_count=int(values[0]),
                input_tokens=int(values[1]),
                cached_input_tokens=int(values[2]),
                output_tokens=int(values[3]),
                cost=Decimal(values[4]),
            )
            for key, values in sorted(aggregates.items())
        )
        total = sum((item.cost for item in breakdown), Decimal("0"))
        remaining = (
            max(Decimal("0"), budget.amount - total)
            if budget is not None
            else None
        )
        return FinOpsSummary(
            tenant_id=tenant_id,
            currency=currency,
            period_start=period_start,
            period_end=period_end,
            total_cost=total,
            budget_id=budget.id if budget is not None else None,
            budget_amount=budget.amount if budget is not None else None,
            remaining_amount=remaining,
            priced_event_count=priced_event_count,
            unpriced_event_count=unpriced_event_count,
            breakdown=breakdown,
        )

    def _spent(
        self,
        tenant_id: str,
        currency: str,
        period_start: datetime,
        period_end: datetime,
    ) -> Decimal:
        total = Decimal("0")
        for event in self._repository.list_llm_usage_events(tenant_id):
            if period_start <= event.created_at < period_end:
                cost = _event_cost(event, currency)
                if cost is not None:
                    total += cost
        return total


def _event_cost(event: LLMUsageEvent, currency: str) -> Decimal | None:
    if event.currency != currency:
        return None
    value = event.actual_cost if event.actual_cost is not None else event.estimated_cost
    if value is None:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def _month_bounds(value: datetime) -> tuple[datetime, datetime]:
    if value.tzinfo is None:
        raise ValueError("FinOps timestamps must be timezone-aware")
    start = value.astimezone(UTC).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
