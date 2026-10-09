#!/usr/bin/env python3
"""Provisions cluster infrastructure using OpenTofu, initializes Kubernetes contexts, and verifies GitOps bootstrap."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from infra.terraform.lifecycle.cluster_common import (
    AVAILABILITY_PROFILES,
    DEFAULT_AVAILABILITY,
    DEFAULT_TARGET,
    DEFAULT_UP_TIMEOUT,
    ensure_deployment_dir,
    find_repo_root,
    get_context_for_target,
    run_tofu_apply,
    run_tofu_init,
    wait_for_argocd_sync,
)
from infra.tools.cloud_emulator.access import cluster_tailnet, kubeconfig
from infra.tools.cloud_emulator.auth import headscale_keys
from infra.tools.cloud_emulator.engine import compose, docker, readiness

# gazelle:include_dep //src/infra/tools/cloud_emulator/access:kubeconfig
# gazelle:include_dep @rules_python//python/runfiles
# gazelle:include_dep //src/infra/tools/cloud_emulator/auth:headscale_keys
from python.runfiles import runfiles  # gazelle:ignore python.runfiles

# LINT.IfChange(local-origin-registry-publication-root)
LOCAL_WORKLOAD_REGISTRY = "127.0.0.1:15100/000000000000/us-east-1"
# LINT.ThenChange(//src/infra/mise.toml:local-origin-registry-publication-root)

# LINT.IfChange(seed-rlocationpath-variable)
SEED_RLOCATIONPATH_VARIABLE = "CLUSTER_SEED_RLOCATIONPATH"
# LINT.ThenChange(//src/infra/terraform/lifecycle/BUILD.bazel:seed-rlocationpath-variable)

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class ClusterUpConfig:
    """Configuration options for cluster bring-up."""

    target: str = DEFAULT_TARGET
    availability: str = DEFAULT_AVAILABILITY
    timeout: int = DEFAULT_UP_TIMEOUT
    skip_wait: bool = False
    context: str | None = None
    with_tailnet: bool = True


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for cluster bring-up."""
    parser = argparse.ArgumentParser(
        description="Bring up cluster infrastructure and verify GitOps bootstrap."
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=f"Deployment target, e.g. local or production (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--availability",
        choices=AVAILABILITY_PROFILES,
        default=DEFAULT_AVAILABILITY,
        help=f"Fleet availability profile ({', '.join(AVAILABILITY_PROFILES)}) (default: {DEFAULT_AVAILABILITY})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_UP_TIMEOUT,
        help=f"Maximum seconds to wait for Argo CD sync (default: {DEFAULT_UP_TIMEOUT})",
    )
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="Skip waiting for Argo CD sync",
    )
    parser.add_argument(
        "--no-tailnet",
        action="store_true",
        help="Skip automatic userspace Tailnet bring-up on local targets",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Override kubectl context (default: derived from target, e.g. ctrl-eaws-lh1 for local)",
    )
    return parser.parse_args(args)


def cluster_up(config: ClusterUpConfig | None = None) -> None:
    """Execute the complete cluster bring-up workflow."""
    cfg = config or ClusterUpConfig()
    # 1. Preflight check
    if cfg.target == "local":
        root = find_repo_root()
        docker.ensure_docker_local(root, require_capacity=True)
        compose.start(root, timeout=cfg.timeout)

    # 2. OpenTofu execution
    target_dir = ensure_deployment_dir(cfg.target)
    run_tofu_init(target_dir)
    run_tofu_apply(target_dir, cfg.availability)
    if cfg.target == "local":
        compose.reconcile_registry(root)
        kubeconfig.project(root)
        seed_local_images()
        readiness.reconcile_tls(root, timeout=cfg.timeout)
        headscale_keys.reconcile(root, timeout=cfg.timeout)

    # 3. GitOps bootstrap verification
    if not cfg.skip_wait:
        ctx = cfg.context or get_context_for_target(cfg.target)
        wait_for_argocd_sync(ctx, timeout=float(cfg.timeout))

    # 4. Local Tailnet mesh enrollment
    if cfg.target == "local" and cfg.with_tailnet:
        try:
            tailnet = cluster_tailnet.TailnetManager()
            tailnet.up()
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                f"Tailnet enrollment failed with exit status {error.returncode}"
            ) from None


def seed_local_images() -> None:
    """Populate the local fleet's registry using the declared Bazel image publisher."""
    files = runfiles.Create()
    location = os.environ.get(SEED_RLOCATIONPATH_VARIABLE)
    if files is None or not location:
        raise RuntimeError("Local image seeding requires the Bazel cluster_up target's runfiles")
    executable = files.Rlocation(location)
    if not executable or not Path(executable).is_file():
        raise RuntimeError("Local image seeding executable is missing from Bazel runfiles")
    environment = {
        **os.environ,
        **files.EnvVars(),
        "WORKLOAD_REGISTRY": LOCAL_WORKLOAD_REGISTRY,
        "WORKLOAD_REGISTRY_INSECURE": "true",
    }
    subprocess.run([executable], check=True, env=environment)


def main(args: Sequence[str] | None = None) -> int:
    """CLI entry point for cluster bring-up."""
    parsed = parse_args(args)
    try:
        cluster_up(
            ClusterUpConfig(
                target=parsed.target,
                availability=parsed.availability,
                timeout=parsed.timeout,
                skip_wait=parsed.skip_wait,
                context=parsed.context,
                with_tailnet=not parsed.no_tailnet,
            )
        )
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"Cluster bring-up failed: {error}", file=sys.stderr)
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main())
