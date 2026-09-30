"""Executes distributed PyTorch neural network training and periodic model checkpointing using Ray Train."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

# gazelle:ignore ray
# gazelle:ignore torch
import ray
import torch  # ty: ignore[unresolved-import]
from infra.tools.otel_tracing.python.tracer import flush_tracing, init_tracing, trace_span
from infra.tools.s3_resolver.client.python.client import resolve_s3_uri
from ray import train
from ray.air.config import RunConfig, ScalingConfig
from ray.train import Checkpoint
from ray.train.torch import get_device, prepare_model
from ray.train.torch.torch_trainer import TorchTrainer

RESULT_PREFIX = "RAY_TRAIN_RESULT="


def completion_record(
    metrics: dict[str, object],
    checkpoint_path: str,
    canonical_prefix: str,
) -> str:
    """Return the machine-readable successful-training result."""
    loss = metrics.get("loss")
    if isinstance(loss, bool) or not isinstance(loss, (int, float)) or not math.isfinite(loss):
        raise RuntimeError("Ray Train did not report a finite loss")
    normalized_checkpoint_path = checkpoint_path.rstrip("/")
    if not normalized_checkpoint_path.startswith(f"{canonical_prefix}/"):
        raise RuntimeError("Ray Train checkpoint escaped the canonical run directory")
    return json.dumps(
        {
            "checkpoint_path": normalized_checkpoint_path,
            "loss": loss,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def train_loop() -> None:
    """Fit one linear layer and report a portable checkpoint."""
    device = get_device()
    model = prepare_model(torch.nn.Linear(1, 1).to(device))
    compiled_model = torch.compile(model)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
    features = torch.linspace(0, 1, 16, dtype=torch.float32, device=device).reshape(-1, 1)
    targets = features * 2 + 1

    epoch_count = 30
    for _epoch in range(epoch_count):
        optimizer.zero_grad()
        loss = torch.nn.functional.mse_loss(compiled_model(features), targets)
        loss.backward()
        optimizer.step()

    with tempfile.TemporaryDirectory() as directory:
        checkpoint_path = Path(directory) / "model.pt"
        torch.save(model.module.state_dict(), checkpoint_path)
        train.report(
            {"epoch": epoch_count, "loss": loss.item()},
            checkpoint=Checkpoint.from_directory(directory),
        )


def main() -> None:
    storage_path = os.environ.get("RAY_TRAIN_STORAGE_PATH", "s3://global/home/examples").rstrip("/")
    run_id = os.environ.get("RAY_TRAIN_RUN_ID", "default")
    workers = int(os.environ.get("RAY_TRAIN_WORKERS", "2"))

    run_storage_path = f"{storage_path}/ray-train"

    init_tracing("ray-train")
    try:
        with trace_span("ray_train_execution", {"run_id": run_id, "workers": workers}):
            target = resolve_s3_uri(run_storage_path)

            ray.init(address="auto")
            result = TorchTrainer(
                train_loop,
                scaling_config=ScalingConfig(num_workers=workers, use_gpu=False),
                run_config=RunConfig(
                    name=run_id,
                    storage_path=target.uri,
                    storage_filesystem=target.pyarrow_filesystem(),
                ),
            ).fit()

            if result.checkpoint is None or result.metrics is None:
                raise RuntimeError("Ray Train did not produce a checkpoint or metrics")

            # Map physical checkpoint path back to canonical logical run URI for acceptance tracking
            result.checkpoint.path.replace(target.uri, run_storage_path)
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
