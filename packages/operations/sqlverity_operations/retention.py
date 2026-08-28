from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import datetime

from packages.catalog.sqlverity_catalog.config import load_catalog_repository_from_environment
from packages.catalog.sqlverity_catalog.repository import (
    OperationalRetentionReport,
)
from packages.connectors.sqlverity_connectors.connection import (
    load_secret_resolver_from_environment,
)


class RetentionConfigurationError(RuntimeError):
    pass


def _parse_cutoff(value: str) -> datetime:
    try:
        cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("cutoff must be an ISO-8601 timestamp") from error
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include a timezone")
    return cutoff


def _payload(report: OperationalRetentionReport, *, applied: bool) -> str:
    return json.dumps(
        {
            "actor_id": report.actor_id,
            "applied": applied,
            "background_jobs": report.background_jobs,
            "completed_at": (
                report.completed_at.isoformat() if report.completed_at is not None else None
            ),
            "cutoff": report.cutoff.isoformat(),
            "quota_windows": report.quota_windows,
            "run_id": report.run_id,
        },
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or apply SQLVerity AI operational-data retention"
    )
    parser.add_argument("command", choices=("preview", "apply"))
    parser.add_argument("--before", type=_parse_cutoff, required=True)
    parser.add_argument(
        "--confirm-before",
        type=_parse_cutoff,
        help="Required for apply; must exactly match --before",
    )
    parser.add_argument(
        "--actor-id",
        default=os.environ.get("SQLVERITY_RETENTION_ACTOR_ID", "retention-cli"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "apply" and args.confirm_before != args.before:
        raise RetentionConfigurationError(
            "Apply requires --confirm-before to exactly match --before"
        )
    repository, _backend = load_catalog_repository_from_environment(
        load_secret_resolver_from_environment(),
        application_name="sqlverity-retention",
    )
    try:
        repository.initialize()
        if args.command == "preview":
            report = repository.preview_operational_retention(args.before)
            applied = False
        else:
            report = repository.purge_operational_records(
                args.before,
                actor_id=args.actor_id,
            )
            applied = True
    finally:
        repository.close()
    print(_payload(report, applied=applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
