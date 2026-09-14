from __future__ import annotations

import asyncio
import unittest
from unittest.mock import PropertyMock, patch

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.security.sqlverity_security import RequestQuotaLimits, RequestQuotaManager, ScopeQuota
from packages.security.sqlverity_security.quota import RequestQuotaLeaseLostError


class RequestLeaseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.addCleanup(self.repository.close)
        self.manager = RequestQuotaManager(
            self.repository,
            RequestQuotaLimits(
                window_seconds=60,
                user=ScopeQuota(100, 1),
                tenant=ScopeQuota(100, 1),
                data_source=ScopeQuota(100, 1),
                lease_seconds=3,
            ),
        )
        self.args = dict(principal_id="user", tenant_id="tenant", data_source_id="source")

    async def test_renewal_continues_until_body_completes_then_releases(self) -> None:
        decision = self.manager.acquire(**self.args)
        assert decision.lease is not None
        response_started, finish_body = asyncio.Event(), asyncio.Event()

        async def response() -> None:
            response_started.set()
            await finish_body.wait()

        with (
            patch.object(
                RequestQuotaManager,
                "renewal_interval_seconds",
                new_callable=PropertyMock,
                return_value=0.01,
            ),
            patch.object(self.manager, "renew", wraps=self.manager.renew) as renew,
        ):
            task = asyncio.create_task(self.manager.run_with_lease(decision.lease, response))
            try:
                await response_started.wait()
                async with asyncio.timeout(5):
                    while renew.call_count < 2:
                        await asyncio.sleep(0.01)
                self.assertFalse(self.manager.acquire(**self.args).allowed)
            finally:
                finish_body.set()
                await task
        self.assertTrue(self.manager.acquire(**self.args).allowed)

    async def test_lease_loss_cancels_response_and_releases_all_scopes(self) -> None:
        decision = self.manager.acquire(**self.args)
        assert decision.lease is not None
        cancelled = asyncio.Event()

        async def response() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(
                RequestQuotaManager,
                "renewal_interval_seconds",
                new_callable=PropertyMock,
                return_value=0.01,
            ),
            patch.object(self.manager, "renew", return_value=False),
        ):
            with self.assertRaises(RequestQuotaLeaseLostError):
                await self.manager.run_with_lease(decision.lease, response)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.manager.acquire(**self.args).allowed)

    async def test_request_cancellation_releases_lease(self) -> None:
        decision = self.manager.acquire(**self.args)
        assert decision.lease is not None
        started = asyncio.Event()

        async def response() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.manager.run_with_lease(decision.lease, response))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.manager.acquire(**self.args).allowed)
