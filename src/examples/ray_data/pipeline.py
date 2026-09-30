"""Implements a Ray Data pipeline reading partitioned Parquet datasets, applying PyTorch transforms, and saving outputs."""

from __future__ import annotations

import os
from typing import Any

import ray

# gazelle:ignore torch
import torch  # ty: ignore[unresolved-import]
from infra.tools.otel_tracing.python.tracer import flush_tracing, init_tracing, trace_span
from infra.tools.s3_resolver.client.python.client import resolve_s3_uri

ROWS = 100


class ComputeTransformStage:
    """Actor stage executed on dedicated compute-worker nodes using PyTorch with device fallback."""

    def __init__(self) -> None:
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def __call__(self, batch: dict[str, list[Any]]) -> dict[str, list[Any]]:
        values = batch.get("value", [])
        tensor = torch.tensor(values, dtype=torch.float32, device=self.device)
        transformed = (tensor * 2.0 + 1.0).to("cpu").tolist()
        return {
            **batch,
            "result": transformed,
        }


def run_pipeline(bucket_uri: str, partition: str, run_id: str) -> None:
    """Execute the data processing pipeline across resolved physical S3 coordinates."""
    input_uri = f"{bucket_uri}/input/daily/{partition}/{run_id}/"
    output_uri = f"{bucket_uri}/output/ray-data/{partition}/{run_id}/"

    with trace_span(
        "run_pipeline", {"bucket_uri": bucket_uri, "partition": partition, "run_id": run_id}
    ):
        resolved_in = resolve_s3_uri(input_uri)
        resolved_out = resolve_s3_uri(output_uri)

        # Seed input partition across parallel blocks within the daily partition.
        (
            ray.data
            .from_items([{"id": row} for row in range(ROWS)])
            .map(lambda row: {"id": row["id"], "value": row["id"] * 2})
            .write_parquet(
                resolved_in.uri,
                filesystem=resolved_in.pyarrow_filesystem(),
                try_create_dir=False,
            )
        )

        ds = ray.data.read_parquet(
            resolved_in.uri,
            filesystem=resolved_in.pyarrow_filesystem(),
        )
        # Dynamic automatic batching balances batch sizing against compute actor utilization.
        ds = ds.map_batches(
            ComputeTransformStage,
            batch_size="auto",
            compute=ray.data.ActorPoolStrategy(),
        )
        ds.write_parquet(
            resolved_out.uri,
            filesystem=resolved_out.pyarrow_filesystem(),
            try_create_dir=False,
        )


def main() -> None:
    bucket_uri = os.environ.get("RAY_DATA_BUCKET_URI", "s3://global/home/examples").rstrip("/")
    partition = os.environ.get("RAY_DATA_PARTITION_KEY", "2026-08-16")
    run_id = os.environ.get("RAY_DATA_RUN_ID", "default")

    init_tracing("ray-data")
    try:
        ray.init(address="auto")
        run_pipeline(bucket_uri, partition, run_id)
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
