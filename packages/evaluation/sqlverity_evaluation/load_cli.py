from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .load import HTTPLoadRunner, LoadTestThresholds, load_test_passes


def _bounded_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be numeric") from error
    if result < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a bounded GET-only SQLVerity AI load profile")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--path", default="/health/ready")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=_bounded_float, default=10.0)
    parser.add_argument("--maximum-error-rate", type=_bounded_float, default=0.01)
    parser.add_argument("--maximum-throttle-rate", type=_bounded_float, default=0.10)
    parser.add_argument("--maximum-p95-ms", type=_bounded_float, default=2_000)
    parser.add_argument("--allow-insecure", action="store_true")
    parser.add_argument("--write-report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_base_url(args.base_url, allow_insecure=args.allow_insecure, parser=parser)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    try:
        thresholds = LoadTestThresholds(
            maximum_error_rate=args.maximum_error_rate,
            maximum_throttle_rate=args.maximum_throttle_rate,
            maximum_p95_ms=args.maximum_p95_ms,
        )
        headers = {}
        token = os.environ.get("SQLVERITY_LOAD_TEST_BEARER_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with httpx.Client(
            base_url=args.base_url.rstrip("/"),
            headers=headers,
            timeout=args.timeout_seconds,
            follow_redirects=False,
        ) as client:
            report = HTTPLoadRunner(client).run(
                args.path,
                request_count=args.requests,
                concurrency=args.concurrency,
            )
    except (ValueError, httpx.HTTPError) as error:
        parser.error(str(error))
    passed = load_test_passes(report, thresholds)
    payload = {
        "passed": passed,
        "profile": asdict(report),
        "thresholds": asdict(thresholds),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.write_report is not None:
        args.write_report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if passed else 1


def _validate_base_url(
    value: str,
    *,
    allow_insecure: bool,
    parser: argparse.ArgumentParser,
) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an HTTP(S) origin")
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not loopback and not allow_insecure:
        parser.error("Remote load targets require HTTPS or --allow-insecure")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        parser.error("--base-url must be an origin without credentials or a path")


if __name__ == "__main__":
    raise SystemExit(main())
