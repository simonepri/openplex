"""Implements a Ray Serve HTTP deployment executing distributed PyTorch model inference with health probes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

# gazelle:ignore ray
# gazelle:ignore starlette
# gazelle:ignore starlette.requests
# gazelle:ignore starlette.requests.Request
# gazelle:ignore starlette.responses
# gazelle:ignore starlette.responses.JSONResponse
# gazelle:ignore torch
import torch  # ty: ignore[unresolved-import]
from infra.tools.otel_tracing.python.tracer import init_tracing, trace_span
from ray import serve  # ty: ignore[unresolved-import]
from starlette.responses import JSONResponse  # ty: ignore[unresolved-import]

if TYPE_CHECKING:
    from starlette.requests import Request  # ty: ignore[unresolved-import]


REQUIRED_FEATURES_COUNT = 2


class _RayStartParams(Protocol):
    ray_client_server_port: int | None


def configure_ray_start(ray_params: _RayStartParams, head: bool) -> None:
    """Disable the unused Ray Client server on Serve heads."""
    if head:
        ray_params.ray_client_server_port = None


def _parse_features(payload: object) -> tuple[float, float]:
    """Parse one finite, two-feature prediction request."""
    if not isinstance(payload, Mapping) or set(payload) != {"features"}:
        raise ValueError("request must contain only features")
    features = payload["features"]
    if not isinstance(features, list) or len(features) != REQUIRED_FEATURES_COUNT:
        raise ValueError("features must contain exactly two numbers")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in features):
        raise ValueError("features must contain exactly two numbers")
    parsed = (float(features[0]), float(features[1]))
    if not all(math.isfinite(value) for value in parsed):
        raise ValueError("features must be finite")
    return parsed


class Predictor:
    """Returns a deterministic score while exercising TorchInductor caching."""

    def __init__(self) -> None:
        init_tracing("ray-serve")
        model = torch.nn.Linear(2, 1)
        with torch.no_grad():
            model.weight.copy_(torch.tensor([[0.25, 0.75]]))
            model.bias.copy_(torch.tensor([0.5]))
        self.model = torch.compile(model.eval())

    async def __call__(self, request: Request) -> JSONResponse:
        with trace_span("predict_inference", {"http.method": request.method}):
            try:
                features = _parse_features(await request.json())
            except (ValueError, TypeError):
                return JSONResponse({"error": "invalid prediction request"}, status_code=400)
            with torch.inference_mode():
                score = self.model(torch.tensor([features], dtype=torch.float32)).item()
            return JSONResponse({"score": score})


application = serve.deployment(
    Predictor,
    autoscaling_config={
        "min_replicas": 0,
        "max_replicas": 1,
        "downscale_to_zero_delay_s": 60,
    },
    ray_actor_options={"num_cpus": 1},
    health_check_period_s=10,
    health_check_timeout_s=5,
).bind()


if __name__ == "__main__":
    serve.run(application)
