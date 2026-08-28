from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from packages.connectors.sqlverity_connectors.connection import (
    SecretResolutionError,
    load_secret_resolver_from_environment,
)

from .golden import GoldenDatasetError, load_dataset, load_predictions
from .live import (
    LiveCertificationRunner,
    PostgreSQLLiveExecutor,
    live_report_payload,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure SQLVerity AI execution accuracy against a live PostgreSQL fixture"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--secret-ref", required=True)
    parser.add_argument("--minimum-execution-accuracy", type=float, default=0.90)
    parser.add_argument("--timeout-seconds", type=int, default=10)
    parser.add_argument("--max-rows", type=int, default=10_000)
    parser.add_argument("--write-report", type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.minimum_execution_accuracy <= 1:
        parser.error("--minimum-execution-accuracy must be between 0 and 1")
    try:
        dataset = load_dataset(args.dataset)
        predictions = load_predictions(args.predictions, dataset)
        secret = load_secret_resolver_from_environment().resolve_postgresql(
            args.secret_ref
        )
        with PostgreSQLLiveExecutor(
            secret.as_connect_kwargs(application_name="sqlverity-live-certification"),
            timeout_seconds=args.timeout_seconds,
            max_rows=args.max_rows,
        ) as executor:
            report = LiveCertificationRunner().run(dataset, predictions, executor)
        payload = {
            "passed": (
                report.metrics.execution_accuracy
                >= args.minimum_execution_accuracy
            ),
            "minimum_execution_accuracy": args.minimum_execution_accuracy,
            "report": live_report_payload(report),
        }
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        print(rendered)
        if args.write_report is not None:
            args.write_report.write_text(rendered + "\n", encoding="utf-8")
        return 0 if payload["passed"] else 1
    except (GoldenDatasetError, SecretResolutionError, RuntimeError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
