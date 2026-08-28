from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import time
from typing import Protocol

from packages.domain.sqlverity_domain.models import utc_now


class RequestQuotaRepository(Protocol):
    def try_acquire_request_quota(
        self,
        *,
        scope_key: str,
        window_number: int,
        max_requests: int,
        max_concurrent: int,
        updated_at: datetime,
    ) -> tuple[bool, str | None]: ...

    def release_request_quota(
        self,
        scope_key: str,
        window_number: int,
        updated_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ScopeQuota:
    requests_per_window: int
    max_concurrent: int

    def __post_init__(self) -> None:
        if self.requests_per_window < 1 or self.max_concurrent < 1:
            raise ValueError("Request quota limits must be positive")


@dataclass(frozen=True, slots=True)
class RequestQuotaLimits:
    window_seconds: int
    user: ScopeQuota
    tenant: ScopeQuota
    data_source: ScopeQuota

    def __post_init__(self) -> None:
        if not 1 <= self.window_seconds <= 3_600:
            raise ValueError("Request quota window must be between 1 and 3600 seconds")


@dataclass(frozen=True, slots=True)
class RequestQuotaLease:
    scope_keys: tuple[str, ...]
    window_number: int


@dataclass(frozen=True, slots=True)
class RequestQuotaDecision:
    allowed: bool
    lease: RequestQuotaLease | None = None
    denied_scope: str | None = None
    reason: str | None = None
    retry_after_seconds: int = 0


class RequestQuotaManager:
    """Database-coordinated rate and concurrency quotas for multiple API instances."""

    def __init__(
        self,
        repository: RequestQuotaRepository,
        limits: RequestQuotaLimits,
        *,
        epoch_clock: Callable[[], float] = time,
        utc_clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._limits = limits
        self._epoch_clock = epoch_clock
        self._utc_clock = utc_clock

    def acquire(
        self,
        *,
        principal_id: str,
        tenant_id: str | None,
        data_source_id: str | None,
    ) -> RequestQuotaDecision:
        now_epoch = max(0, int(self._epoch_clock()))
        window_number = now_epoch // self._limits.window_seconds
        retry_after = max(
            1,
            ((window_number + 1) * self._limits.window_seconds) - now_epoch,
        )
        scopes: list[tuple[str, ScopeQuota, str]] = [
            (f"user:{principal_id}", self._limits.user, "user")
        ]
        if tenant_id is not None:
            scopes.append((f"tenant:{tenant_id}", self._limits.tenant, "tenant"))
        if tenant_id is not None and data_source_id is not None:
            scopes.append(
                (
                    f"data-source:{tenant_id}:{data_source_id}",
                    self._limits.data_source,
                    "data_source",
                )
            )
        acquired: list[str] = []
        for scope_key, quota, scope_name in scopes:
            allowed, reason = self._repository.try_acquire_request_quota(
                scope_key=scope_key,
                window_number=window_number,
                max_requests=quota.requests_per_window,
                max_concurrent=quota.max_concurrent,
                updated_at=self._utc_clock(),
            )
            if not allowed:
                self._release_keys(tuple(reversed(acquired)), window_number)
                return RequestQuotaDecision(
                    allowed=False,
                    denied_scope=scope_name,
                    reason=reason,
                    retry_after_seconds=retry_after if reason == "rate" else 1,
                )
            acquired.append(scope_key)
        return RequestQuotaDecision(
            allowed=True,
            lease=RequestQuotaLease(tuple(acquired), window_number),
        )

    def release(self, lease: RequestQuotaLease) -> None:
        self._release_keys(tuple(reversed(lease.scope_keys)), lease.window_number)

    def _release_keys(self, scope_keys: tuple[str, ...], window_number: int) -> None:
        for scope_key in scope_keys:
            self._repository.release_request_quota(
                scope_key,
                window_number,
                self._utc_clock(),
            )
