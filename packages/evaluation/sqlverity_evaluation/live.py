from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from importlib import import_module
from math import ceil
from time import perf_counter
from typing import Any, Protocol

import sqlglot

from .golden import (
    GoldenDataset,
    GoldenDisposition,
    GoldenProposal,
    GoldenRunner,
)


@dataclass(frozen=True, slots=True)
class QueryExecutionSample:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    latency_ms: int
    truncated: bool = False


class LiveSQLExecutor(Protocol):
    def execute(self, sql: str) -> QueryExecutionSample: ...


@dataclass(frozen=True, slots=True)
class LiveCaseExecution:
    case_id: str
    passed: bool
    prediction_executed: bool
    reference_executed: bool
    latency_ms: int | None
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class LiveCertificationMetrics:
    eligible_case_count: int
    execution_passed_count: int
    execution_accuracy: float
    latency_p50_ms: int | None
    latency_p95_ms: int | None


@dataclass(frozen=True, slots=True)
class LiveCertificationReport:
    dataset_id: str
    dataset_version: int
    dataset_sha256: str
    metrics: LiveCertificationMetrics
    cases: tuple[LiveCaseExecution, ...]


class LiveCertificationRunner:
    def run(
        self,
        dataset: GoldenDataset,
        predictions: dict[str, GoldenProposal],
        executor: LiveSQLExecutor,
    ) -> LiveCertificationReport:
        offline = GoldenRunner().run(dataset, predictions)
        offline_by_id = {case.case_id: case for case in offline.cases}
        results: list[LiveCaseExecution] = []
        latencies: list[int] = []
        for case in dataset.cases:
            if case.expected_outcome is not GoldenDisposition.ACCEPTED:
                continue
            prediction = predictions[case.id]
            offline_case = offline_by_id[case.id]
            if not offline_case.validator_accepted or prediction.needs_clarification:
                results.append(
                    LiveCaseExecution(
                        case_id=case.id,
                        passed=False,
                        prediction_executed=False,
                        reference_executed=False,
                        latency_ms=None,
                        failure="prediction_not_safe_to_execute",
                    )
                )
                continue
            try:
                reference = executor.execute(case.reference_proposal.sql)
            except Exception:
                results.append(
                    LiveCaseExecution(
                        case_id=case.id,
                        passed=False,
                        prediction_executed=False,
                        reference_executed=False,
                        latency_ms=None,
                        failure="reference_execution_failed",
                    )
                )
                continue
            try:
                predicted = executor.execute(prediction.sql)
            except Exception:
                results.append(
                    LiveCaseExecution(
                        case_id=case.id,
                        passed=False,
                        prediction_executed=False,
                        reference_executed=True,
                        latency_ms=None,
                        failure="prediction_execution_failed",
                    )
                )
                continue
            latencies.append(predicted.latency_ms)
            truncated = reference.truncated or predicted.truncated
            ordered = _has_order_by(case.reference_proposal.sql)
            equal = not truncated and _equivalent_rows(reference, predicted, ordered=ordered)
            results.append(
                LiveCaseExecution(
                    case_id=case.id,
                    passed=equal,
                    prediction_executed=True,
                    reference_executed=True,
                    latency_ms=predicted.latency_ms,
                    failure=("result_mismatch_or_truncated" if not equal else None),
                )
            )
        passed = sum(result.passed for result in results)
        count = len(results)
        return LiveCertificationReport(
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            dataset_sha256=dataset.sha256,
            metrics=LiveCertificationMetrics(
                eligible_case_count=count,
                execution_passed_count=passed,
                execution_accuracy=passed / count if count else 0.0,
                latency_p50_ms=_percentile(latencies, 0.50),
                latency_p95_ms=_percentile(latencies, 0.95),
            ),
            cases=tuple(results),
        )


class PostgreSQLLiveExecutor:
    def __init__(
        self,
        connect_kwargs: Mapping[str, object],
        *,
        timeout_seconds: int = 10,
        max_rows: int = 10_000,
    ) -> None:
        if not 1 <= timeout_seconds <= 300 or not 1 <= max_rows <= 100_000:
            raise ValueError("Invalid live certification execution bounds")
        try:
            psycopg_module = import_module("psycopg")
            self._connection = psycopg_module.connect(
                **dict(connect_kwargs),
                options="-c default_transaction_read_only=on",
            )
        except (AttributeError, ImportError) as error:
            raise RuntimeError("The postgres extra is required for live certification") from error
        self._timeout_ms = timeout_seconds * 1_000
        self._max_rows = max_rows

    def __enter__(self) -> PostgreSQLLiveExecutor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def execute(self, sql: str) -> QueryExecutionSample:
        started_at = perf_counter()
        with self._connection.transaction():
            self._connection.execute(
                f"SET LOCAL statement_timeout = {self._timeout_ms}"
            )
            cursor = self._connection.execute(sql)
            rows = cursor.fetchmany(self._max_rows + 1)
            columns = tuple(column.name for column in cursor.description or ())
        truncated = len(rows) > self._max_rows
        bounded_rows = rows[: self._max_rows]
        return QueryExecutionSample(
            columns=columns,
            rows=tuple(tuple(row) for row in bounded_rows),
            latency_ms=max(0, int((perf_counter() - started_at) * 1_000)),
            truncated=truncated,
        )


def live_report_payload(report: LiveCertificationReport) -> dict[str, Any]:
    return asdict(report)


def _equivalent_rows(
    reference: QueryExecutionSample,
    predicted: QueryExecutionSample,
    *,
    ordered: bool,
) -> bool:
    if len(reference.columns) != len(predicted.columns):
        return False
    reference_rows = tuple(_canonical_row(row) for row in reference.rows)
    predicted_rows = tuple(_canonical_row(row) for row in predicted.rows)
    if ordered:
        return reference_rows == predicted_rows
    return Counter(reference_rows) == Counter(predicted_rows)


def _canonical_row(row: Sequence[Any]) -> tuple[str, ...]:
    return tuple(_canonical_value(value) for value in row)


def _canonical_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if value is None:
        return "<null>"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return repr(value)


def _has_order_by(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return False
    return expression.args.get("order") is not None


def _percentile(values: Sequence[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, ceil(len(ordered) * quantile) - 1)
    return int(ordered[index])
