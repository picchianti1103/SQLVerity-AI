from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from .models import new_id, utc_now


class LLMBudgetUnavailableError(RuntimeError):
    pass


class LLMBudgetReservationConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LLMBudgetReservation:
    tenant_id: str
    provider_id: str
    model_id: str
    pricing_id: str
    currency: str
    amount: Decimal
    period_start: datetime
    period_end: datetime
    id: str = field(default_factory=new_id)
    state: str = "reserved"
    actual_cost: Decimal | None = None
    usage_event_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.amount.is_finite() or self.amount < 0:
            raise ValueError("Reservation amount must be finite and nonnegative")
        if (self.period_start, self.period_end) != month_bounds(self.created_at):
            raise ValueError("Reservation period must contain its creation time in UTC")
        if self.updated_at.tzinfo is None:
            raise ValueError("Reservation timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class LLMBudgetBalance:
    spent: Decimal = Decimal("0")
    reserved: Decimal = Decimal("0")
    uncertain: Decimal = Decimal("0")
    reconciled: Decimal = Decimal("0")


def month_bounds(value: datetime) -> tuple[datetime, datetime]:
    if value.tzinfo is None:
        raise ValueError("FinOps timestamps must be timezone-aware")
    start = value.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end
