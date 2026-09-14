from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import time
from typing import Protocol
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

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
        lease_id: str,
        expires_at: datetime,
    ) -> tuple[bool, str | None]: ...

    def release_request_quota(
        self,
        scope_key: str,
        lease_id: str,
        updated_at: datetime,
    ) -> None: ...

    def renew_request_quota(
        self,
        scope_keys: tuple[str, ...],
        lease_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool: ...


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
    lease_seconds: int = 120

    def __post_init__(self) -> None:
        if not 3 <= self.lease_seconds <= 3_600:
            raise ValueError("Request lease duration must be between 3 and 3600 seconds")
        if not 1 <= self.window_seconds <= 3_600:
            raise ValueError("Request quota window must be between 1 and 3600 seconds")


@dataclass(frozen=True, slots=True)
class RequestQuotaLease:
    scope_keys: tuple[str, ...]
    lease_id: str


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
        lease_id = str(uuid4())
        now = self._utc_clock()
        try:
            for scope_key, quota, scope_name in scopes:
                allowed, reason = self._repository.try_acquire_request_quota(
                    scope_key=scope_key,
                    window_number=window_number,
                    max_requests=quota.requests_per_window,
                    max_concurrent=quota.max_concurrent,
                    updated_at=now,
                    lease_id=lease_id,
                    expires_at=now + timedelta(seconds=self._limits.lease_seconds),
                )
                if not allowed:
                    self._release_keys(tuple(reversed(acquired)), lease_id)
                    acquired.clear()
                    return RequestQuotaDecision(
                        allowed=False,
                        denied_scope=scope_name,
                        reason=reason,
                        retry_after_seconds=retry_after if reason == "rate" else 1,
                    )
                acquired.append(scope_key)
        except BaseException:
            self._release_keys(tuple(reversed(acquired)), lease_id)
            raise
        return RequestQuotaDecision(
            allowed=True,
            lease=RequestQuotaLease(tuple(acquired), lease_id),
        )

    def release(self, lease: RequestQuotaLease) -> None:
        self._release_keys(tuple(reversed(lease.scope_keys)), lease.lease_id)

    @property
    def renewal_interval_seconds(self) -> float:
        return self._limits.lease_seconds / 3

    def renew(self, lease: RequestQuotaLease) -> bool:
        now = self._utc_clock()
        return self._repository.renew_request_quota(
            lease.scope_keys,
            lease.lease_id,
            now,
            now + timedelta(seconds=self._limits.lease_seconds),
        )

    async def run_with_lease(
        self,
        lease: RequestQuotaLease,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        """Renew throughout the ASGI operation, including response streaming."""

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.renewal_interval_seconds)
                try:
                    renewed = await run_in_threadpool(self.renew, lease)
                except Exception as error:
                    raise RequestQuotaLeaseLostError("Request lease renewal failed") from error
                if not renewed:
                    raise RequestQuotaLeaseLostError("Request concurrency lease expired")

        async def run_operation() -> None:
            await operation()

        work = asyncio.create_task(run_operation())
        renewal = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait((work, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            await work
        finally:
            renewal.cancel()
            work.cancel()
            await asyncio.gather(renewal, work, return_exceptions=True)
            await run_in_threadpool(self.release, lease)

    def _release_keys(self, scope_keys: tuple[str, ...], lease_id: str) -> None:
        for scope_key in scope_keys:
            self._repository.release_request_quota(
                scope_key,
                lease_id,
                self._utc_clock(),
            )


class RequestQuotaLeaseLostError(RuntimeError):
    """Stop admitting work when its concurrency lease cannot be maintained."""
