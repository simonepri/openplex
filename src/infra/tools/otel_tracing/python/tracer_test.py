"""Tests that workload spans reach the OTLP traces endpoint with the workload resource identity."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from opentelemetry.sdk.trace.export import SpanExportResult
from src.infra.tools.otel_tracing.python.tracer import flush_tracing, init_tracing, trace_span


class TracerTest(unittest.TestCase):
    def test_exports_spans_to_the_traces_path_with_workload_identity(self) -> None:
        environment = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318/",
            "TEAM": "ml",
            "ENVIRONMENT": "staging",
        }
        with (
            patch.dict(os.environ, environment),
            patch("src.infra.tools.otel_tracing.python.tracer.OTLPSpanExporter") as exporter,
        ):
            exporter.return_value.export.return_value = SpanExportResult.SUCCESS
            init_tracing("trainer")
            with trace_span("step", {"step": 1}):
                pass
            flush_tracing()

        exporter.assert_called_once_with(endpoint="http://collector:4318/v1/traces")
        spans = [
            span for call in exporter.return_value.export.call_args_list for span in call.args[0]
        ]
        self.assertEqual([span.name for span in spans], ["step"])
        self.assertEqual(spans[0].attributes, {"step": 1})
        resource = spans[0].resource.attributes
        self.assertEqual(
            (resource["service.name"], resource["team"], resource["environment"]),
            ("trainer", "ml", "staging"),
        )


if __name__ == "__main__":
    unittest.main()
