from __future__ import annotations

import unittest
from datetime import UTC, datetime

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.security.sqlverity_security import (
    RequestQuotaLimits,
    RequestQuotaManager,
    ScopeQuota,
)


class RequestQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.now = 120.0
        self.manager = RequestQuotaManager(
            self.repository,
            RequestQuotaLimits(
                window_seconds=60,
                user=ScopeQuota(requests_per_window=2, max_concurrent=1),
                tenant=ScopeQuota(requests_per_window=10, max_concurrent=5),
                data_source=ScopeQuota(requests_per_window=10, max_concurrent=5),
            ),
            epoch_clock=lambda: self.now,
            utc_clock=lambda: datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_concurrency_is_released_and_rate_limit_remains_counted(self) -> None:
        first = self.manager.acquire(
            principal_id="user-1",
            tenant_id="tenant-1",
            data_source_id="source-1",
        )
        concurrent = self.manager.acquire(
            principal_id="user-1",
            tenant_id="tenant-1",
            data_source_id="source-1",
        )
        assert first.lease is not None
        self.manager.release(first.lease)
        second = self.manager.acquire(
            principal_id="user-1",
            tenant_id="tenant-1",
            data_source_id="source-1",
        )
        assert second.lease is not None
        self.manager.release(second.lease)
        rate_limited = self.manager.acquire(
            principal_id="user-1",
            tenant_id="tenant-1",
            data_source_id="source-1",
        )

        self.assertTrue(first.allowed)
        self.assertFalse(concurrent.allowed)
        self.assertEqual("concurrency", concurrent.reason)
        self.assertTrue(second.allowed)
        self.assertFalse(rate_limited.allowed)
        self.assertEqual("rate", rate_limited.reason)

    def test_new_window_resets_request_count(self) -> None:
        for _ in range(2):
            decision = self.manager.acquire(
                principal_id="user-1",
                tenant_id=None,
                data_source_id=None,
            )
            assert decision.lease is not None
            self.manager.release(decision.lease)
        self.now = 180.0

        reset = self.manager.acquire(
            principal_id="user-1",
            tenant_id=None,
            data_source_id=None,
        )

        self.assertTrue(reset.allowed)
        assert reset.lease is not None
        self.manager.release(reset.lease)

    def test_new_window_recovers_crashed_lease_and_ignores_late_release(self) -> None:
        crashed = self.manager.acquire(
            principal_id="user-1",
            tenant_id=None,
            data_source_id=None,
        )
        assert crashed.lease is not None
        self.now = 180.0

        recovered = self.manager.acquire(
            principal_id="user-1",
            tenant_id=None,
            data_source_id=None,
        )
        assert recovered.lease is not None
        self.manager.release(crashed.lease)
        still_concurrent = self.manager.acquire(
            principal_id="user-1",
            tenant_id=None,
            data_source_id=None,
        )

        self.assertTrue(recovered.allowed)
        self.assertFalse(still_concurrent.allowed)
        self.assertEqual("concurrency", still_concurrent.reason)
        self.manager.release(recovered.lease)


if __name__ == "__main__":
    unittest.main()
