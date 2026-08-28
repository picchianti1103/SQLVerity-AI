from __future__ import annotations

import unittest

import httpx

from packages.evaluation.sqlverity_evaluation.load import (
    HTTPLoadRunner,
    LoadTestThresholds,
    load_test_passes,
)


class HTTPLoadRunnerTests(unittest.TestCase):
    def test_report_accounts_for_success_throttling_and_server_errors(self) -> None:
        statuses = iter((200, 401, 429, 500))

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("GET", request.method)
            return httpx.Response(next(statuses))

        with httpx.Client(
            base_url="https://sqlverity.example",
            transport=httpx.MockTransport(handler),
        ) as client:
            report = HTTPLoadRunner(client).run(
                "/health/ready",
                request_count=4,
                concurrency=1,
            )

        self.assertEqual({"200": 1, "401": 1, "429": 1, "500": 1}, report.status_counts)
        self.assertEqual(0.5, report.error_rate)
        self.assertEqual(0.25, report.throttle_rate)
        self.assertFalse(
            load_test_passes(
                report,
                LoadTestThresholds(
                    maximum_error_rate=0.1,
                    maximum_throttle_rate=0.1,
                    maximum_p95_ms=10_000,
                ),
            )
        )

    def test_runner_rejects_external_or_query_paths(self) -> None:
        with httpx.Client(base_url="https://sqlverity.example") as client:
            runner = HTTPLoadRunner(client)
            with self.assertRaises(ValueError):
                runner.run(
                    "https://other.example/health",
                    request_count=1,
                    concurrency=1,
                )
            with self.assertRaises(ValueError):
                runner.run(
                    "/v1/system/capabilities?secret=value",
                    request_count=1,
                    concurrency=1,
                )
            with self.assertRaises(ValueError):
                runner.run(
                    "/health-not-real",
                    request_count=1,
                    concurrency=1,
                )


if __name__ == "__main__":
    unittest.main()
