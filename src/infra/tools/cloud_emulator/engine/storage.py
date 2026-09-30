"""Docker volume enumeration, retention, and cleanup for local clusters."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, TypeVar

from infra.tools.cloud_emulator.engine.compose import (
    Fleet,
    configuration,
    owned_containers,
    run,
)

if TYPE_CHECKING:
    from pathlib import Path

_T = TypeVar("_T")


def _dispatch(name: str, fallback: _T) -> _T:  # ruff: ignore[non-pep695-generic-function]
    facade = sys.modules.get("infra.tools.cloud_emulator.runtime")
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)  # ty: ignore[unsound-return-statement]
    return fallback


def cluster_volumes(root: Path) -> list[str]:
    """Name the volumes holding this fleet's EKS cluster data, attached or orphaned."""
    get_config = _dispatch("configuration", configuration)
    get_owned = _dispatch("owned_containers", owned_containers)
    run_cmd = _dispatch("run", run)

    fleet = get_config(root)
    prefixes = (
        f"floci-aws-{fleet.namespace}-eks-",
        f"floci-{fleet.namespace}-eks-",
    )
    attached = {
        mount["Name"]
        for item in get_owned(fleet)
        if any(item["Name"].startswith(f"/{prefix}") for prefix in prefixes)
        for mount in item.get("Mounts") or []
        if mount["Type"] == "volume"
    }
    volumes = run_cmd(["docker", "volume", "ls", "--format", "{{.Name}}"]).stdout.splitlines()
    return sorted(
        attached | {name for name in volumes if any(name.startswith(prefix) for prefix in prefixes)}
    )


def remove_volumes(names: list[str]) -> None:
    """Delete cluster volumes that Floci retains after deleting their clusters."""
    run_cmd = _dispatch("run", run)
    volumes = set(run_cmd(["docker", "volume", "ls", "--format", "{{.Name}}"]).stdout.splitlines())
    remaining = [name for name in names if name in volumes]
    if remaining:
        print(f"Removing {len(remaining)} retained cluster volumes...", flush=True)
        run_cmd(["docker", "volume", "rm", *remaining], timeout=300)


def ensure_storage(fleet: Fleet, children: list[dict[str, Any]]) -> None:
    """Ensure persistent Docker volumes and dedicated emulator network exist."""
    run_cmd = _dispatch("run", run)
    volumes = run_cmd(["docker", "volume", "ls", "--format", "{{.Name}}"]).stdout.splitlines()
    if fleet.volume not in volumes:
        if children:
            raise RuntimeError(
                "Floci metadata volume is missing while retained service containers exist"
            )
        run_cmd(["docker", "volume", "create", fleet.volume])
    if fleet.headscale_volume and fleet.headscale_volume not in volumes:
        run_cmd(["docker", "volume", "create", fleet.headscale_volume])
    networks = run_cmd(["docker", "network", "ls", "--format", "{{.Name}}"]).stdout.splitlines()
    if fleet.network not in networks:
        run_cmd([
            "docker",
            "network",
            "create",
            "--subnet",
            "172.19.0.0/16",
            "--ip-range",
            # LINT.IfChange(local_dynamic_address_pool)
            "172.19.1.0/24",
            # LINT.ThenChange(//src/infra/terraform/deployments/local/deployment.yaml:local_dynamic_address_pool,//src/infra/terraform/deployments/local/deployment.yaml:local_dynamic_address_pool)
            "--gateway",
            "172.19.0.1",
            fleet.network,
        ])
