"""Initializes OpenTelemetry tracer providers and context managers for workloads."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

if TYPE_CHECKING:
    from collections.abc import Iterator

_STATE = {"initialized": False}


def init_tracing(service_name: str | None = None) -> trace.Tracer:
    """Initialize OpenTelemetry tracer provider if not already initialized."""
    svc_name = service_name or os.environ.get("OTEL_SERVICE_NAME", "workload")

    if not _STATE["initialized"]:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        if not endpoint.endswith("/v1/traces"):
            endpoint = f"{endpoint.rstrip('/')}/v1/traces"

        resource = Resource.create({
            "service.name": svc_name,
            "team": os.environ.get("TEAM", "examples"),
            "environment": os.environ.get("ENVIRONMENT", "production"),
        })

        provider = TracerProvider(resource=resource)
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        _STATE["initialized"] = True

    return trace.get_tracer(svc_name)


def flush_tracing() -> None:
    """Flush pending spans and shut down the active TracerProvider."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush()
        provider.shutdown()


@contextmanager
def trace_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[trace.Span]:
    """Execute a code block wrapped in an active OpenTelemetry trace span."""
    tracer = trace.get_tracer(os.environ.get("OTEL_SERVICE_NAME", "workload"))
    with tracer.start_as_current_span(name, attributes=attributes) as span:
        yield span
