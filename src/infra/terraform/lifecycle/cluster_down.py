#!/usr/bin/env python3
"""Tears down local Docker fleet containers or destroys remote OpenTofu cluster infrastructure."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import TYPE_CHECKING

from infra.terraform.lifecycle.cluster_common import (
    DEFAULT_DOWN_TIMEOUT,
    DEFAULT_TARGET,
    ensure_deployment_dir,
    find_repo_root,
    run_tofu_destroy,
    run_tofu_init,
    tofu_environment,
)
from infra.tools.cloud_emulator import runtime
from infra.tools.cloud_emulator.access import cluster_tailnet

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for fleet shutdown or destruction."""
    parser = argparse.ArgumentParser(
        description="Stop the local fleet, or destroy infrastructure for cloud targets."
    )
    parser.add_argument(
        "--destroy",
        action="store_true",
        help="Destroy local infrastructure and its cluster data instead of retaining it",
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=f"Deployment target (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_DOWN_TIMEOUT,
        help=f"Maximum seconds to wait (default: {DEFAULT_DOWN_TIMEOUT})",
    )
    return parser.parse_args(args)


def cluster_down(
    target: str = DEFAULT_TARGET,
    timeout: int = DEFAULT_DOWN_TIMEOUT,
    repo_root: Path | None = None,
    *,
    destroy: bool = False,
) -> None:
    """Stop local containers while retaining state, or explicitly destroy infrastructure."""
    root = repo_root or find_repo_root()
    if target == "local":
        try:
            tailnet = cluster_tailnet.TailnetManager(repo_root=root)
            tailnet.down()
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass

        if not destroy:
            runtime.stop(root, timeout=timeout)
            return

        runtime.ensure_docker_local(root)
        runtime.start(root, timeout=timeout)

    target_dir = ensure_deployment_dir(target, repo_root)
    run_tofu_init(target_dir)
    volumes: list[str] = []
    if target == "local":
        forget_floci_runtime(target_dir, timeout)
        # Floci retains cluster volumes on delete, so a recreated cluster would resume old state.
        volumes = runtime.cluster_volumes(root)
    run_tofu_destroy(target_dir, timeout=float(timeout))
    if target == "local":
        runtime.stop(root, timeout=timeout)
        runtime.remove_volumes(volumes)


def forget_floci_runtime(target_dir: Path, timeout: int) -> None:
    """Remove historical emulator ownership before a destroy-mode plan."""
    command = ["tofu", f"-chdir={target_dir}", "state"]
    environment = tofu_environment(target_dir)
    current = subprocess.run(
        [*command, "pull"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if not current.stdout.strip():
        return
    resources = json.loads(current.stdout).get("resources", [])
    if any(resource.get("module") == "module.floci_runtime" for resource in resources):
        subprocess.run(
            [*command, "rm", "module.floci_runtime"],
            check=True,
            timeout=timeout,
            env=environment,
        )


def main(args: Sequence[str] | None = None) -> int:
    """CLI entry point for fleet shutdown or destruction."""
    parsed = parse_args(args)
    try:
        cluster_down(
            target=parsed.target,
            timeout=parsed.timeout,
            destroy=parsed.destroy,
        )
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as err:
        print(f"cluster_down failed: {err}", file=sys.stderr)
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main())
