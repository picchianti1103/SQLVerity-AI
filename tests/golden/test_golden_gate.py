from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from packages.connectors.sqlverity_connectors.ddl import PostgreSQLDDLParser
from packages.evaluation.sqlverity_evaluation import (
    GoldenDatasetError,
    GoldenRunner,
    evaluate_gate,
    load_baseline,
    load_dataset,
    load_thresholds,
)
from packages.evaluation.sqlverity_evaluation.cli import main

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = REPOSITORY_ROOT / "fixtures" / "questions" / "golden_v1.json"
THRESHOLDS_PATH = (
    REPOSITORY_ROOT / "fixtures" / "questions" / "golden_thresholds_v1.json"
)
BASELINE_PATH = (
    REPOSITORY_ROOT / "fixtures" / "questions" / "golden_baseline_v1.json"
)


class GoldenGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = load_dataset(DATASET_PATH)
        self.thresholds = load_thresholds(THRESHOLDS_PATH)
        self.baseline = load_baseline(BASELINE_PATH)

    def test_committed_dataset_has_required_coverage(self) -> None:
        outcomes = [case.expected_outcome.value for case in self.dataset.cases]

        self.assertEqual(len(self.dataset.cases), 50)
        self.assertEqual(
            {case.context_id for case in self.dataset.cases},
            {"commerce", "finance", "support"},
        )
        self.assertEqual(outcomes.count("accepted"), 35)
        self.assertEqual(outcomes.count("clarification"), 8)
        self.assertEqual(outcomes.count("rejected"), 7)

    def test_demo_schema_fixtures_are_valid_and_relational(self) -> None:
        expected_objects = {"commerce.sql": 4, "finance.sql": 4, "support.sql": 4}
        schema_root = REPOSITORY_ROOT / "fixtures" / "schemas"

        for name, object_count in expected_objects.items():
            with self.subTest(schema=name):
                snapshot = PostgreSQLDDLParser().parse(
                    data_source_id=name.removesuffix(".sql"),
                    ddl=(schema_root / name).read_text(encoding="utf-8"),
                )
                self.assertEqual(len(snapshot.objects), object_count)
                self.assertGreaterEqual(len(snapshot.relationships), 3)

    def test_reference_predictions_pass_strict_baseline_gate(self) -> None:
        report = GoldenRunner().run(self.dataset)
        gate = evaluate_gate(report, self.thresholds, self.baseline)

        self.assertTrue(gate.passed)
        self.assertEqual(gate.failures, ())
        self.assertEqual(gate.regressions, ())
        self.assertEqual(report.metrics.passed_count, 50)
        self.assertEqual(report.metrics.safety_rate, 1.0)
        self.assertEqual(report.metrics.clarification_precision, 1.0)
        self.assertIsNone(report.metrics.execution_accuracy)
        self.assertIn(
            "execution_accuracy:no_live_database_results",
            report.unmeasured_metrics,
        )

    def test_gate_detects_a_case_regression(self) -> None:
        predictions = {
            case.id: case.reference_proposal for case in self.dataset.cases
        }
        original = predictions["commerce_001"]
        predictions["commerce_001"] = replace(
            original,
            sql="",
            tables=(),
            columns=(),
            ambiguities=("intentional regression",),
            needs_clarification=True,
        )

        report = GoldenRunner().run(self.dataset, predictions)
        gate = evaluate_gate(report, self.thresholds, self.baseline)

        self.assertFalse(gate.passed)
        self.assertEqual(gate.regressions, ("commerce_001",))
        self.assertIn("regressions:1>0", gate.failures)
        self.assertLess(report.metrics.case_pass_rate, 1.0)

    def test_empty_prediction_set_cannot_fall_back_to_references(self) -> None:
        with self.assertRaisesRegex(GoldenDatasetError, "Prediction ids"):
            GoldenRunner().run(self.dataset, {})

    def test_gate_rejects_stale_runner_baseline(self) -> None:
        report = GoldenRunner().run(self.dataset)
        stale_baseline = replace(self.baseline, runner_version="outdated")

        gate = evaluate_gate(report, self.thresholds, stale_baseline)

        self.assertFalse(gate.passed)
        self.assertIn("baseline_runner_mismatch", gate.failures)

    def test_cli_returns_success_and_machine_readable_report(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--dataset",
                    str(DATASET_PATH),
                    "--thresholds",
                    str(THRESHOLDS_PATH),
                    "--baseline",
                    str(BASELINE_PATH),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn('"passed": true', output.getvalue())


if __name__ == "__main__":
    unittest.main()
