from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import (
    BackgroundJob,
    BackgroundJobStatus,
    DataSourceType,
)
from packages.jobs.sqlverity_jobs import DurableJobWorker, JobExecutionOutcome


class BackgroundJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Acme")
        self.data_source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )

    def tearDown(self) -> None:
        self.repository.close()

    def _job(self) -> BackgroundJob:
        return BackgroundJob(
            tenant_id=self.tenant.id,
            data_source_id=self.data_source.id,
            job_type="semantic_inference",
            payload={"provider_id": "fake"},
        )

    def test_enqueue_is_idempotent_while_a_matching_job_is_active(self) -> None:
        first = self.repository.enqueue_background_job(self._job())
        second = self.repository.enqueue_background_job(self._job())

        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(self.repository.list_background_jobs(self.tenant.id)))

    def test_expired_lease_is_recovered_by_another_worker(self) -> None:
        job = self.repository.enqueue_background_job(self._job())
        now = max(datetime(2026, 8, 22, tzinfo=UTC), job.scheduled_at)
        first_claim = self.repository.claim_background_job(
            worker_id="worker-one",
            now=now,
            lease_seconds=15,
        )
        assert first_claim is not None

        self.assertIsNone(
            self.repository.claim_background_job(
                worker_id="worker-two",
                now=now + timedelta(seconds=14),
                lease_seconds=15,
            )
        )
        recovered = self.repository.claim_background_job(
            worker_id="worker-two",
            now=now + timedelta(seconds=16),
            lease_seconds=15,
        )

        assert recovered is not None
        self.assertEqual(job.id, recovered.id)
        self.assertEqual("worker-two", recovered.worker_id)
        self.assertEqual(2, recovered.attempt_count)

    def test_worker_completes_and_enqueues_a_bounded_continuation(self) -> None:
        self.repository.enqueue_background_job(self._job())
        calls: list[str] = []

        def handler(job: BackgroundJob) -> JobExecutionOutcome:
            calls.append(job.id)
            return JobExecutionOutcome(
                result={"proposal_count": 10},
                continuation_payload=(
                    {"provider_id": "fake", "after_object_ref": "public.orders"}
                    if len(calls) == 1
                    else None
                ),
            )

        worker = DurableJobWorker(
            self.repository,
            {"semantic_inference": handler},
            worker_id="test-worker",
            lease_seconds=30,
        )

        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        jobs = self.repository.list_background_jobs(self.tenant.id)

        self.assertEqual(2, len(jobs))
        self.assertTrue(all(job.status is BackgroundJobStatus.SUCCEEDED for job in jobs))
        self.assertEqual(2, len(calls))

    def test_failure_is_retried_without_storing_exception_text(self) -> None:
        queued = self.repository.enqueue_background_job(self._job())

        def handler(_job: BackgroundJob) -> JobExecutionOutcome:
            raise RuntimeError("credential-value-that-must-not-be-stored")

        worker = DurableJobWorker(
            self.repository,
            {"semantic_inference": handler},
            worker_id="test-worker",
            lease_seconds=30,
        )
        self.assertTrue(worker.run_once())
        failed = self.repository.get_background_job(self.tenant.id, queued.id)

        assert failed is not None
        self.assertEqual(BackgroundJobStatus.QUEUED, failed.status)
        self.assertEqual("RuntimeError", failed.last_error_code)
        self.assertNotIn(
            "credential-value",
            str(self.repository.audit_events(self.tenant.id)),
        )


if __name__ == "__main__":
    unittest.main()
