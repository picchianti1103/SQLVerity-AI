from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .golden import (
    GoldenDatasetError,
    GoldenRunner,
    baseline_payload,
    build_baseline,
    evaluate_gate,
    load_baseline,
    load_dataset,
    load_predictions,
    load_thresholds,
    report_payload,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SQLVerity AI golden regression gate")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--write-report", type=Path)
    parser.add_argument("--write-baseline", type=Path)
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.dataset)
        predictions = (
            load_predictions(args.predictions, dataset)
            if args.predictions is not None
            else None
        )
        report = GoldenRunner().run(dataset, predictions)
        thresholds = load_thresholds(args.thresholds)
        baseline = load_baseline(args.baseline) if args.baseline is not None else None
        gate = evaluate_gate(report, thresholds, baseline)
        output = {
            "gate": {
                "passed": gate.passed,
                "failures": gate.failures,
                "regressions": gate.regressions,
            },
            "report": report_payload(report),
        }
        rendered = json.dumps(output, indent=2, sort_keys=True)
        print(rendered)
        if args.write_report is not None:
            args.write_report.write_text(rendered + "\n", encoding="utf-8")
        if args.write_baseline is not None:
            args.write_baseline.write_text(
                json.dumps(
                    baseline_payload(build_baseline(report)),
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        return 0 if gate.passed else 1
    except GoldenDatasetError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
