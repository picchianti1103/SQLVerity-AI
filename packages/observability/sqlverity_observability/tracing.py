from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse


class TracingConfigurationError(RuntimeError):
    pass


class RequestTrace(Protocol):
    @property
    def trace_id(self) -> str | None: ...

    def finish(self, *, route: str, status_code: int) -> Mapping[str, str]: ...


class RequestTracer(Protocol):
    def start_request(self, headers: Mapping[str, str], *, method: str) -> RequestTrace: ...

    def shutdown(self) -> None: ...


@dataclass(slots=True)
class _DisabledRequestTrace:
    @property
    def trace_id(self) -> str | None:
        return None

    def finish(self, *, route: str, status_code: int) -> Mapping[str, str]:
        return {}


class DisabledRequestTracer:
    def start_request(
        self,
        headers: Mapping[str, str],
        *,
        method: str,
    ) -> RequestTrace:
        return _DisabledRequestTrace()

    def shutdown(self) -> None:
        return None


@dataclass(slots=True)
class _OpenTelemetryRequestTrace:
    method: str
    span: Any
    span_context_manager: Any
    propagator: Any
    finished: bool = False

    @property
    def trace_id(self) -> str | None:
        span_context = self.span.get_span_context()
        if not span_context.is_valid:
            return None
        return f"{span_context.trace_id:032x}"

    def finish(self, *, route: str, status_code: int) -> Mapping[str, str]:
        if self.finished:
            return {}
        self.finished = True
        self.span.update_name(f"{self.method} {route}")
        self.span.set_attribute("http.route", route)
        self.span.set_attribute("http.response.status_code", status_code)
        if status_code >= 500:
            from opentelemetry.trace import Status, StatusCode

            self.span.set_status(Status(StatusCode.ERROR))
        carrier: dict[str, str] = {}
        self.propagator.inject(carrier)
        # The middleware deliberately does not attach exceptions, URLs, query strings,
        # identities, SQL, or prompts to spans.
        self.span_context_manager.__exit__(None, None, None)
        return carrier


class OpenTelemetryRequestTracer:
    def __init__(self, provider: Any, tracer: Any, propagator: Any) -> None:
        self._provider = provider
        self._tracer = tracer
        self._propagator = propagator

    def start_request(
        self,
        headers: Mapping[str, str],
        *,
        method: str,
    ) -> RequestTrace:
        from opentelemetry.trace import SpanKind

        normalized_method = method.upper()
        parent_context = self._propagator.extract(carrier=dict(headers))
        manager = self._tracer.start_as_current_span(
            f"{normalized_method} pending-route",
            context=parent_context,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": normalized_method},
        )
        span = manager.__enter__()
        return _OpenTelemetryRequestTrace(
            method=normalized_method,
            span=span,
            span_context_manager=manager,
            propagator=self._propagator,
        )

    def shutdown(self) -> None:
        self._provider.shutdown()


def load_request_tracer_from_environment() -> RequestTracer:
    if not _environment_boolean("SQLVERITY_OTEL_ENABLED", default=False):
        return DisabledRequestTracer()
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        raise TracingConfigurationError(
            "OTEL_EXPORTER_OTLP_ENDPOINT is required when SQLVERITY_OTEL_ENABLED is true"
        )
    _validate_export_endpoint(endpoint)
    sample_ratio = _sample_ratio()
    service_name = os.environ.get("OTEL_SERVICE_NAME", "sqlverity-api").strip()
    if not service_name or len(service_name) > 100:
        raise TracingConfigurationError("OTEL_SERVICE_NAME is invalid")
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import (
            SERVICE_NAME,
            Resource,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
        )
        from opentelemetry.sdk.trace.sampling import (
            ParentBased,
            TraceIdRatioBased,
        )
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
    except ImportError as error:
        raise TracingConfigurationError(
            "Install the observability extra to enable OpenTelemetry"
        ) from error

    trace_endpoint = endpoint.rstrip("/")
    if not urlparse(trace_endpoint).path.rstrip("/"):
        trace_endpoint = f"{trace_endpoint}/v1/traces"
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: service_name}),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=trace_endpoint))
    )
    return OpenTelemetryRequestTracer(
        provider,
        provider.get_tracer("sqlverity.api", "0.1.0"),
        TraceContextTextMapPropagator(),
    )


def _sample_ratio() -> float:
    value = os.environ.get("SQLVERITY_OTEL_TRACE_SAMPLE_RATIO", "0.1").strip()
    try:
        ratio = float(value)
    except ValueError as error:
        raise TracingConfigurationError("SQLVERITY_OTEL_TRACE_SAMPLE_RATIO is invalid") from error
    if not 0 <= ratio <= 1:
        raise TracingConfigurationError("SQLVERITY_OTEL_TRACE_SAMPLE_RATIO must be between 0 and 1")
    return ratio


def _validate_export_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TracingConfigurationError("OTLP endpoint must be an HTTP(S) URL")
    allow_insecure = _environment_boolean("SQLVERITY_OTEL_ALLOW_INSECURE", default=False)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not loopback and not allow_insecure:
        raise TracingConfigurationError(
            "Remote OTLP endpoints require HTTPS or SQLVERITY_OTEL_ALLOW_INSECURE=true"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TracingConfigurationError("OTLP endpoint must not contain credentials or a query")


def _environment_boolean(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise TracingConfigurationError(f"{name} must be a boolean")
