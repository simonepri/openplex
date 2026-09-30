"""Executes scheduled housekeeping operations, logging run timestamps and generating demo batch execution metrics."""

from __future__ import annotations

import datetime
import os
import sys

from infra.tools.otel_tracing.python.tracer import flush_tracing, init_tracing, trace_span


def run_task(timestamp: datetime.datetime | None = None) -> dict[str, str]:
    # The Ray base runs Python 3.10, before datetime.UTC was added.
    now = datetime.datetime.now(datetime.timezone.utc) if timestamp is None else timestamp  # ruff: ignore[datetime-timezone-utc]
    run_id = os.environ.get("CRON_RUN_ID", now.strftime("%Y%m%d%H%M%S"))
    with trace_span("run_task", {"cron.run_id": run_id}):
        return {
            "executed_at": now.isoformat(),
            "run_id": run_id,
            "status": "completed",
        }


def main() -> None:
    init_tracing("batch-cron")
    try:
        with trace_span("batch_cron_execution"):
            result = run_task()
            sys.stdout.write(f"Batch cron task completed successfully: {result}\n")
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
