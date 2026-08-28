from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from packages.evaluation.sqlverity_evaluation import (
    GoldenDataset,
    LiveCertificationRunner,
    QueryExecutionSample,
    load_dataset,
)


class DeterministicExecutor:
    def execute(self, sql: str) -> QueryExecutionSample:
        value = 999 if "2025-01-01" in sql else sum(sql.encode("utf-8"))
        return QueryExecutionSample(
            columns=("value",),
            rows=((value,),),
            latency_ms=10,
        )


class LiveCertificationTests(unittest.TestCase):
    dataset: GoldenDataset

    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(Path("fixtures/questions/golden_v1.json"))

    def test_reference_predictions_measure_full_execution_accuracy(self) -> None:
        predictions = {
            case.id: case.reference_proposal for case in self.dataset.cases
        }

        report = LiveCertificationRunner().run(
            self.dataset,
            predictions,
            DeterministicExecutor(),
        )

        self.assertEqual(35, report.metrics.eligible_case_count)
        self.assertEqual(1.0, report.metrics.execution_accuracy)
        self.assertEqual(10, report.metrics.latency_p95_ms)

    def test_safe_but_wrong_result_reduces_execution_accuracy(self) -> None:
        predictions = {
            case.id: case.reference_proposal for case in self.dataset.cases
        }
        predictions["commerce_001"] = replace(
            predictions["commerce_001"],
            sql=(
                "SELECT COUNT(*) AS order_count FROM commerce.orders "
                "WHERE ordered_at >= DATE '2025-01-01' "
                "AND ordered_at < DATE '2026-01-01'"
            ),
        )

        report = LiveCertificationRunner().run(
            self.dataset,
            predictions,
            DeterministicExecutor(),
        )

        failed = next(case for case in report.cases if case.case_id == "commerce_001")
        self.assertFalse(failed.passed)
        self.assertEqual(34 / 35, report.metrics.execution_accuracy)


if __name__ == "__main__":
    unittest.main()
