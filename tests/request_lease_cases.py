from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.security.sqlverity_security import RequestQuotaLimits, RequestQuotaManager, ScopeQuota


class RequestLeaseCases(unittest.TestCase):
    def repositories(self) -> tuple[SQLiteCatalogRepository, SQLiteCatalogRepository]:
        raise NotImplementedError

    def setUp(self) -> None:
        self.repository, self.other = self.repositories()
        self.addCleanup(self.repository.close)
        self.addCleanup(self.other.close)
        self.repository.initialize()
        self.now = 179.0
        self.args = dict(
            principal_id=str(uuid4()), tenant_id=str(uuid4()), data_source_id=str(uuid4())
        )
        self.manager = self.make_manager(self.repository)
        self.other_manager = self.make_manager(self.other)

    def make_manager(self, repository: SQLiteCatalogRepository) -> RequestQuotaManager:
        return RequestQuotaManager(
            repository,
            RequestQuotaLimits(
                window_seconds=60,
                user=ScopeQuota(10, 1),
                tenant=ScopeQuota(20, 2),
                data_source=ScopeQuota(20, 2),
                lease_seconds=120,
            ),
            epoch_clock=lambda: self.now,
            utc_clock=lambda: datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=self.now),
        )

    def test_rollover_keeps_active_lease_and_double_release_does_not_free_another(self) -> None:
        first = self.manager.acquire(**self.args)
        assert first.lease is not None
        self.now = 180
        self.assertFalse(self.other_manager.acquire(**self.args).allowed)
        self.manager.release(first.lease)
        second = self.other_manager.acquire(**self.args)
        assert second.lease is not None
        self.manager.release(first.lease)
        self.assertFalse(self.manager.acquire(**self.args).allowed)
        self.other_manager.release(second.lease)

    def test_expired_lease_cannot_be_renewed_or_release_its_replacement(self) -> None:
        first = self.manager.acquire(**self.args)
        assert first.lease is not None
        self.now += 120
        self.assertFalse(self.manager.renew(first.lease))
        replacement = self.other_manager.acquire(**self.args)
        assert replacement.lease is not None
        self.manager.release(first.lease)
        self.assertFalse(self.manager.acquire(**self.args).allowed)
        self.other_manager.release(replacement.lease)

    def test_renewal_keeps_all_scopes_occupied_across_multiple_windows(self) -> None:
        first = self.manager.acquire(**self.args)
        assert first.lease is not None
        for _ in range(5):
            self.now += 60
            self.assertTrue(self.manager.renew(first.lease))
            self.assertFalse(self.other_manager.acquire(**self.args).allowed)
        self.manager.release(first.lease)
        self.assertTrue(self.other_manager.acquire(**self.args).allowed)

    def test_two_instances_compete_for_one_concurrency_slot(self) -> None:
        barrier = Barrier(2)

        def attempt(manager: RequestQuotaManager) -> bool:
            barrier.wait(timeout=10)
            return manager.acquire(**self.args).allowed

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (self.manager, self.other_manager)))
        self.assertEqual([False, True], sorted(outcomes))

    def test_partial_acquisition_error_releases_earlier_scopes(self) -> None:
        original = self.repository.try_acquire_request_quota

        def acquire(**kwargs: Any) -> tuple[bool, str | None]:
            if str(kwargs["scope_key"]).startswith("tenant:"):
                raise RuntimeError("storage failure")
            return original(**kwargs)

        with patch.object(self.repository, "try_acquire_request_quota", side_effect=acquire):
            with self.assertRaises(RuntimeError):
                self.manager.acquire(**self.args)
        self.assertTrue(self.other_manager.acquire(**self.args).allowed)
