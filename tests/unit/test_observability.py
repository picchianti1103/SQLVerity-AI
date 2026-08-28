from __future__ import annotations

import unittest

from packages.observability.sqlverity_observability import (
    OperationalMetrics,
    RequestObservation,
)


class OperationalMetricsTests(unittest.TestCase):
    def test_prometheus_metrics_use_bounded_route_labels(self) -> None:
        metrics = OperationalMetrics()
        metrics.begin_request()
        metrics.end_request(
            RequestObservation(
                method="get",
                route="/v1/tenants/{tenant_id}",
                status_code=200,
                elapsed_seconds=0.125,
                request_id="request-1",
            )
        )

        rendered = metrics.render_prometheus()

        self.assertIn("sqlverity_http_requests_active 0", rendered)
        self.assertIn('method="GET",route="/v1/tenants/{tenant_id}"', rendered)
        self.assertIn('status="200"} 1', rendered)
        self.assertIn('le="0.25"} 1', rendered)
        self.assertIn('le="0.1"} 0', rendered)
        self.assertIn("sqlverity_http_request_duration_seconds_count", rendered)
        self.assertIn("0.125000000", rendered)


if __name__ == "__main__":
    unittest.main()
