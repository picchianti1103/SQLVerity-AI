from __future__ import annotations

import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import perf_counter

import httpx


@dataclass(frozen=True, slots=True)
class LoadTestReport:
    request_count: int
    concurrency: int
    elapsed_seconds: float
    requests_per_second: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    error_rate: float
    throttle_rate: float
    status_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class LoadTestThresholds:
    maximum_error_rate: float = 0.01
    maximum_throttle_rate: float = 0.10
    maximum_p95_ms: float = 2_000

    def __post_init__(self) -> None:
        if not 0 <= self.maximum_error_rate <= 1:
            raise ValueError("Maximum error rate must be between zero and one")
        if not 0 <= self.maximum_throttle_rate <= 1:
            raise ValueError("Maximum throttle rate must be between zero and one")
        if self.maximum_p95_ms <= 0:
            raise ValueError("Maximum p95 latency must be positive")


class HTTPLoadRunner:
    """Run a bounded GET-only profile without retaining response content."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def run(
        self,
        path: str,
        *,
        request_count: int,
        concurrency: int,
    ) -> LoadTestReport:
        if not 1 <= request_count <= 100_000:
            raise ValueError("Request count must be between 1 and 100000")
        if not 1 <= concurrency <= min(request_count, 1_000):
            raise ValueError("Concurrency is outside the safe bounded range")
        _validate_safe_path(path)
        started = perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            samples = tuple(executor.map(lambda _: self._sample(path), range(request_count)))
        elapsed = max(perf_counter() - started, 0.000_001)
        latencies = sorted(sample[1] for sample in samples)
        statuses = Counter(sample[0] for sample in samples)
        failures = sum(
            count
            for status_code, count in statuses.items()
            if status_code == "transport_error"
            or (
                status_code != "429"
                and not 200 <= int(status_code) < 300
            )
        )
        throttles = statuses.get("429", 0)
        return LoadTestReport(
            request_count=request_count,
            concurrency=concurrency,
            elapsed_seconds=elapsed,
            requests_per_second=request_count / elapsed,
            latency_p50_ms=_percentile(latencies, 0.50),
            latency_p95_ms=_percentile(latencies, 0.95),
            latency_p99_ms=_percentile(latencies, 0.99),
            error_rate=failures / request_count,
            throttle_rate=throttles / request_count,
            status_counts=dict(sorted(statuses.items())),
        )

    def _sample(self, path: str) -> tuple[str, float]:
        started = perf_counter()
        try:
            with self._client.stream("GET", path) as response:
                status_code = str(response.status_code)
        except httpx.HTTPError:
            status_code = "transport_error"
        return status_code, (perf_counter() - started) * 1_000


def load_test_passes(
    report: LoadTestReport,
    thresholds: LoadTestThresholds,
) -> bool:
    return (
        report.error_rate <= thresholds.maximum_error_rate
        and report.throttle_rate <= thresholds.maximum_throttle_rate
        and report.latency_p95_ms <= thresholds.maximum_p95_ms
    )


def _validate_safe_path(path: str) -> None:
    supported_prefix = path in {"/health", "/health/ready"} or path.startswith("/v1/")
    if (
        not supported_prefix
        or "?" in path
        or "#" in path
        or "//" in path
    ):
        raise ValueError("Load-test path must be a local SQLVerity AI health endpoint or v1 path")


def _percentile(values: list[float], quantile: float) -> float:
    index = max(0, math.ceil(len(values) * quantile) - 1)
    return values[index]
