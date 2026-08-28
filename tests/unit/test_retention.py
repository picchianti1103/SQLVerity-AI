from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import BackgroundJob


class OperationalRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")

    def tearDown(self) -> None:
        self.repository.close()

    def test_preview_then_purge_only_terminal_and_inactive_records(self) -> None:
        old = datetime.now(UTC) - timedelta(days=90)
        cutoff = datetime.now(UTC) - timedelta(days=30)
        self.repository.enqueue_background_job(
            BackgroundJob(
                tenant_id=self.tenant.id,
                job_type="queued_work",
                payload={},
                created_at=old,
                updated_at=old,
                scheduled_at=old,
            )
        )
        self.repository.enqueue_background_job(
            BackgroundJob(
                tenant_id=self.tenant.id,
                job_type="completed_work",
                payload={},
                created_at=old,
                updated_at=old,
                scheduled_at=old,
            )
        )
        for offset in (1, 3):
            claimed = self.repository.claim_background_job(
                worker_id="retention-test-worker",
                now=old + timedelta(seconds=offset),
                lease_seconds=30,
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.repository.complete_background_job(
                claimed.id,
                "retention-test-worker",
                {},
                now=old + timedelta(seconds=offset + 1),
            )
        current = self.repository.enqueue_background_job(
            BackgroundJob(
                tenant_id=self.tenant.id,
                job_type="current_work",
                payload={},
            )
        )
        self.repository.try_acquire_request_quota(
            scope_key="old-window",
            window_number=1,
            max_requests=10,
            max_concurrent=2,
            updated_at=old,
        )
        self.repository.release_request_quota("old-window", 1, old)

        preview = self.repository.preview_operational_retention(cutoff)
        report = self.repository.purge_operational_records(
            cutoff,
            actor_id="test-operator",
        )

        self.assertEqual(2, preview.background_jobs)
        self.assertEqual(1, preview.quota_windows)
        self.assertEqual(2, report.background_jobs)
        self.assertEqual(1, report.quota_windows)
        self.assertIsNotNone(report.run_id)
        self.assertIsNotNone(
            self.repository.get_background_job(self.tenant.id, current.id)
        )


if __name__ == "__main__":
    unittest.main()
