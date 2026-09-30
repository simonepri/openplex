"""Check packaged application imports and OpenTelemetry dependency compatibility in the real image."""

from __future__ import annotations

import importlib
import importlib.metadata
import sys


def main() -> None:
    application = importlib.import_module(sys.argv[1])
    if sys.argv[1] == "examples.ray_serve.app":
        deployment = importlib.import_module("ray.serve.deployment")
        if not isinstance(application.application, deployment.Application):
            raise TypeError("Ray Serve entry point did not produce an Application")
    if (
        sys.argv[1] == "examples.batch_cron.task"
        and application.run_task()["status"] != "completed"
    ):
        raise RuntimeError("Batch task did not complete in the image runtime")
    requirements = importlib.import_module("packaging.requirements")
    distributions = {
        "opentelemetry-api",
        "opentelemetry-exporter-http-transport",
        "opentelemetry-exporter-otlp-common",
        "opentelemetry-exporter-otlp-proto-common",
        "opentelemetry-exporter-otlp-proto-http",
        "opentelemetry-exporter-prometheus",
        "opentelemetry-proto",
        "opentelemetry-sdk",
        "opentelemetry-semantic-conventions",
    }
    for name in sorted(distributions):
        distribution = importlib.metadata.distribution(name)
        for specification in distribution.requires or []:
            requirement = requirements.Requirement(specification)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            installed = importlib.metadata.version(requirement.name)
            if installed not in requirement.specifier:
                raise RuntimeError(f"{name} requires {requirement}; image provides {installed}")
    tracer = importlib.import_module("infra.tools.otel_tracing.python.tracer")
    tracer.init_tracing("image-import-acceptance")
    tracer.flush_tracing()
    print(f"Imported {sys.argv[1]} and verified OpenTelemetry runtime requirements")


if __name__ == "__main__":
    main()
