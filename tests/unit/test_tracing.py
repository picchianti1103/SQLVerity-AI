from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from packages.observability.sqlverity_observability import (
    DisabledRequestTracer,
    TracingConfigurationError,
    load_request_tracer_from_environment,
)


class RequestTracingTests(unittest.TestCase):
    def test_tracing_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            tracer = load_request_tracer_from_environment()

        self.assertIsInstance(tracer, DisabledRequestTracer)
        request_trace = tracer.start_request({}, method="GET")
        self.assertIsNone(request_trace.trace_id)
        self.assertEqual(request_trace.finish(route="/health", status_code=200), {})

    def test_enabled_tracing_requires_an_export_endpoint(self) -> None:
        with (
            patch.dict(os.environ, {"SQLVERITY_OTEL_ENABLED": "true"}, clear=True),
            self.assertRaisesRegex(TracingConfigurationError, "OTEL_EXPORTER_OTLP_ENDPOINT"),
        ):
            load_request_tracer_from_environment()

    def test_remote_plain_http_endpoint_requires_explicit_opt_in(self) -> None:
        environment = {
            "SQLVERITY_OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.internal:4318",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(TracingConfigurationError, "require HTTPS"),
        ):
            load_request_tracer_from_environment()

    def test_enabled_tracing_propagates_w3c_context_without_content(self) -> None:
        from opentelemetry.exporter.otlp.proto.http import trace_exporter
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        environment = {
            "SQLVERITY_OTEL_ENABLED": "true",
            "SQLVERITY_OTEL_TRACE_SAMPLE_RATIO": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "OTEL_SERVICE_NAME": "sqlverity-test",
        }
        parent_trace_id = "0123456789abcdef0123456789abcdef"
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(trace_exporter, "OTLPSpanExporter", return_value=exporter),
        ):
            tracer = load_request_tracer_from_environment()
            request_trace = tracer.start_request(
                {"traceparent": f"00-{parent_trace_id}-0123456789abcdef-01"},
                method="get",
            )
            response_headers = request_trace.finish(
                route="/v1/tenants/{tenant_id}",
                status_code=200,
            )
            tracer.shutdown()

        self.assertEqual(parent_trace_id, request_trace.trace_id)
        self.assertTrue(response_headers["traceparent"].startswith(f"00-{parent_trace_id}-"))
        spans = exporter.get_finished_spans()
        self.assertEqual(1, len(spans))
        self.assertEqual("GET /v1/tenants/{tenant_id}", spans[0].name)
        attributes = spans[0].attributes or {}
        self.assertNotIn("http.url", attributes)
        self.assertNotIn("db.statement", attributes)


if __name__ == "__main__":
    unittest.main()
