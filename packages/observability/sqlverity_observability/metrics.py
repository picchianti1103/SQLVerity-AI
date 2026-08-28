from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock

_LOGGER = logging.getLogger("sqlverity.requests")


@dataclass(frozen=True, slots=True)
class RequestObservation:
    method: str
    route: str
    status_code: int
    elapsed_seconds: float
    request_id: str
    trace_id: str | None = None


class OperationalMetrics:
    """Low-cardinality in-process metrics suitable for Prometheus scraping."""

    _DURATION_BUCKETS = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    )

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_requests = 0
        self._request_counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._request_duration: dict[tuple[str, str], float] = defaultdict(float)
        self._request_duration_counts: dict[tuple[str, str], int] = defaultdict(int)
        self._request_duration_buckets: dict[tuple[str, str], list[int]] = {}

    def begin_request(self) -> None:
        with self._lock:
            self._active_requests += 1

    def end_request(self, observation: RequestObservation) -> None:
        method = observation.method.upper()
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._request_counts[(method, observation.route, observation.status_code)] += 1
            duration = max(0.0, observation.elapsed_seconds)
            duration_key = (method, observation.route)
            self._request_duration[duration_key] += duration
            self._request_duration_counts[duration_key] += 1
            buckets = self._request_duration_buckets.setdefault(
                duration_key,
                [0] * len(self._DURATION_BUCKETS),
            )
            for index, upper_bound in enumerate(self._DURATION_BUCKETS):
                if duration <= upper_bound:
                    buckets[index] += 1
        _LOGGER.info(
            json.dumps(
                {
                    "event": "http.request.completed",
                    "request_id": observation.request_id,
                    "trace_id": observation.trace_id,
                    "method": method,
                    "route": observation.route,
                    "status_code": observation.status_code,
                    "elapsed_ms": round(observation.elapsed_seconds * 1_000, 3),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def render_prometheus(
        self,
        *,
        worker_enabled: bool | None = None,
        worker_healthy: bool | None = None,
        worker_last_poll_age_seconds: float | None = None,
    ) -> str:
        with self._lock:
            active = self._active_requests
            counts = dict(self._request_counts)
            durations = dict(self._request_duration)
            duration_counts = dict(self._request_duration_counts)
            duration_buckets = {
                key: tuple(value) for key, value in self._request_duration_buckets.items()
            }
        lines = [
            "# HELP sqlverity_http_requests_active Requests currently handled by this replica.",
            "# TYPE sqlverity_http_requests_active gauge",
            f"sqlverity_http_requests_active {active}",
            "# HELP sqlverity_http_requests_total Completed HTTP requests.",
            "# TYPE sqlverity_http_requests_total counter",
        ]
        for (method, route, status_code), count in sorted(counts.items()):
            lines.append(
                "sqlverity_http_requests_total"
                f'{{method="{_escape(method)}",route="{_escape(route)}",'
                f'status="{status_code}"}} {count}'
            )
        lines.extend(
            (
                "# HELP sqlverity_http_request_duration_seconds HTTP request duration.",
                "# TYPE sqlverity_http_request_duration_seconds histogram",
            )
        )
        for (method, route), duration in sorted(durations.items()):
            labels = f'method="{_escape(method)}",route="{_escape(route)}"'
            bucket_values = duration_buckets[(method, route)]
            for upper_bound, count in zip(self._DURATION_BUCKETS, bucket_values, strict=True):
                lines.append(
                    "sqlverity_http_request_duration_seconds_bucket"
                    f'{{{labels},le="{upper_bound:g}"}} {count}'
                )
            lines.append(
                "sqlverity_http_request_duration_seconds_bucket"
                f'{{{labels},le="+Inf"}} {duration_counts[(method, route)]}'
            )
            lines.append(
                "sqlverity_http_request_duration_seconds_sum"
                f"{{{labels}}} {duration:.9f}"
            )
            lines.append(
                "sqlverity_http_request_duration_seconds_count"
                f"{{{labels}}} {duration_counts[(method, route)]}"
            )
        if worker_enabled is not None:
            lines.extend(
                (
                    "# HELP sqlverity_background_worker_enabled "
                    "Whether this replica runs a worker.",
                    "# TYPE sqlverity_background_worker_enabled gauge",
                    f"sqlverity_background_worker_enabled {int(worker_enabled)}",
                    "# HELP sqlverity_background_worker_up Whether this replica's worker is alive.",
                    "# TYPE sqlverity_background_worker_up gauge",
                    f"sqlverity_background_worker_up {int(bool(worker_healthy))}",
                )
            )
            if worker_last_poll_age_seconds is not None:
                lines.extend(
                    (
                        "# HELP sqlverity_background_worker_last_poll_age_seconds "
                        "Seconds since the worker last polled the durable queue.",
                        "# TYPE sqlverity_background_worker_last_poll_age_seconds gauge",
                        "sqlverity_background_worker_last_poll_age_seconds "
                        f"{max(0.0, worker_last_poll_age_seconds):.6f}",
                    )
                )
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
