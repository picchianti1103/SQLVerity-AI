from __future__ import annotations

import logging
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import BackgroundJob, utc_now

LOGGER = logging.getLogger("sqlverity.worker")


@dataclass(frozen=True, slots=True)
class JobExecutionOutcome:
    result: Mapping[str, object]
    continuation_payload: Mapping[str, object] | None = None


type JobHandler = Callable[[BackgroundJob], JobExecutionOutcome]


class DurableJobWorker:
    """Lease-based catalog worker safe to run concurrently across API replicas."""

    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        handlers: Mapping[str, JobHandler],
        *,
        poll_seconds: float = 1.0,
        lease_seconds: int = 120,
        worker_id: str | None = None,
    ) -> None:
        if not 0.05 <= poll_seconds <= 60:
            raise ValueError("Worker poll interval is invalid")
        if not 15 <= lease_seconds <= 3600:
            raise ValueError("Worker lease interval is invalid")
        self._repository = repository
        self._handlers = dict(handlers)
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._worker_id = worker_id or _default_worker_id()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._last_poll_monotonic: float | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(
            target=self._run,
            name=f"sqlverity-worker-{self._worker_id[-8:]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)

    def health_check(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def last_poll_age_seconds(self) -> float | None:
        last_poll = self._last_poll_monotonic
        if last_poll is None:
            return None
        return max(0.0, monotonic() - last_poll)

    def run_once(self) -> bool:
        self._last_poll_monotonic = monotonic()
        job = self._repository.claim_background_job(
            worker_id=self._worker_id,
            now=utc_now(),
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return False
        handler = self._handlers.get(job.job_type)
        if handler is None:
            self._repository.fail_background_job(
                job.id,
                self._worker_id,
                "UnsupportedJobType",
                now=utc_now(),
                retry_delay_seconds=0,
            )
            return True

        heartbeat_stop = Event()
        heartbeat = Thread(
            target=self._heartbeat,
            args=(job.id, heartbeat_stop),
            name=f"sqlverity-heartbeat-{job.id[-8:]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            outcome = handler(job)
            self._repository.complete_background_job(
                job.id,
                self._worker_id,
                outcome.result,
                now=utc_now(),
                continuation_payload=outcome.continuation_payload,
            )
        except Exception as error:
            retry_delay = min(300, 2 ** min(job.attempt_count, 8))
            self._repository.fail_background_job(
                job.id,
                self._worker_id,
                type(error).__name__,
                now=utc_now(),
                retry_delay_seconds=retry_delay,
            )
            LOGGER.warning(
                "background job failed",
                extra={
                    "job_id": job.id,
                    "job_type": job.job_type,
                    "error_type": type(error).__name__,
                },
            )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2.0)
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                claimed = self.run_once()
            except Exception as error:
                LOGGER.error(
                    "background worker poll failed",
                    extra={"error_type": type(error).__name__},
                )
                claimed = False
            if not claimed:
                self._stop_event.wait(self._poll_seconds)

    def _heartbeat(self, job_id: str, stop_event: Event) -> None:
        interval = max(5.0, self._lease_seconds / 3)
        while not stop_event.wait(interval):
            try:
                if not self._repository.heartbeat_background_job(
                    job_id,
                    self._worker_id,
                    now=utc_now(),
                    lease_seconds=self._lease_seconds,
                ):
                    return
            except Exception as error:
                LOGGER.warning(
                    "background job heartbeat failed",
                    extra={"job_id": job_id, "error_type": type(error).__name__},
                )


def _default_worker_id() -> str:
    host = socket.gethostname().replace(" ", "-")[:40] or "host"
    return f"{host}-{os.getpid()}-{str(uuid4())[:8]}"
