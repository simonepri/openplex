"""Colima and Docker daemon health, lifecycle, and storage headroom."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, TypeVar

from infra.tools.cloud_emulator.engine.compose import find_repo_root, load_local_deployment

if TYPE_CHECKING:
    from pathlib import Path

# LINT.IfChange(colima-vm-size)
COLIMA_CPUS = 14
COLIMA_DISK_GIB = 256
COLIMA_MEMORY_GIB = 40
# LINT.ThenChange(//src/infra/docs/readme.md:colima-vm-size)
LOCAL_DOCKER_MINIMUM_FREE_PERCENT = 30
LOCAL_NODEFS_EVICTION_PERCENT = 20
GIB = 1024**3

_T = TypeVar("_T")


def _dispatch(name: str, fallback: _T) -> _T:  # ruff: ignore[non-pep695-generic-function]
    facade = sys.modules.get("infra.tools.cloud_emulator.runtime")
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)  # ty: ignore[unsound-return-statement]
    return fallback


def check_docker_running() -> bool:
    """Check if the Docker daemon is responding to 'docker info'."""
    subp = _dispatch("subprocess", subprocess)
    try:
        res = subp.run(
            ["docker", "info"],
            capture_output=True,
            check=False,
            text=True,
        )
        return res.returncode == 0
    except OSError:
        return False


def ensure_docker_local(
    root: Path | None = None,
    *,
    require_capacity: bool = False,
) -> None:
    """Verify Docker is running for local target, starting Colima if necessary."""
    required_gib: int | None = None
    trim_disk = _dispatch("trim_colima_sparse_disk", trim_colima_sparse_disk)
    val_ws = _dispatch(
        "validate_local_workspace_storage_headroom",
        validate_local_workspace_storage_headroom,
    )
    is_docker_running = _dispatch("check_docker_running", check_docker_running)
    val_docker = _dispatch(
        "validate_local_docker_storage_headroom",
        validate_local_docker_storage_headroom,
    )
    repo_root = _dispatch("find_repo_root", find_repo_root)

    if require_capacity:
        trim_disk()
        required_gib = val_ws(root or repo_root())
    if is_docker_running():
        if require_capacity:
            val_docker(required_gib)
        return

    colima_bin = _dispatch("shutil", shutil).which("colima")
    if not colima_bin:
        raise RuntimeError(
            "Docker daemon is not reachable and 'colima' is not installed. "
            "Please start Docker or install Colima."
        )

    print("Docker daemon not reachable; attempting to start Colima...")
    subp = _dispatch("subprocess", subprocess)
    start_res = subp.run(
        [
            "colima",
            "start",
            "--vm-type",
            "vz",
            "--vz-rosetta",
            "--cpu",
            str(COLIMA_CPUS),
            "--memory",
            str(COLIMA_MEMORY_GIB),
            "--disk",
            str(COLIMA_DISK_GIB),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if start_res.returncode != 0:
        err = start_res.stderr.strip() or start_res.stdout.strip()
        raise RuntimeError(f"Failed to start Colima (exit code {start_res.returncode}): {err}")

    if not is_docker_running():
        raise RuntimeError(
            "Colima was started, but Docker daemon remains unreachable via 'docker info'."
        )
    if require_capacity:
        val_docker(required_gib)
    print("Colima started successfully; Docker daemon is ready.")


def trim_colima_sparse_disk() -> None:
    """Reclaim unallocated space in Colima's APFS sparse disk before checking headroom."""
    if not _dispatch("shutil", shutil).which("colima"):
        return
    subp = _dispatch("subprocess", subprocess)
    try:
        context = subp.run(
            ["docker", "context", "show"],
            capture_output=True,
            check=False,
            text=True,
        )
        if context.returncode != 0:
            return
        name = context.stdout.strip()
        if name == "colima":
            profile = "default"
        elif name.startswith("colima-"):
            profile = name.removeprefix("colima-")
        else:
            return

        status = subp.run(
            ["colima", "--profile", profile, "status"],
            capture_output=True,
            check=False,
            text=True,
        )
        if status.returncode != 0:
            return

        subp.run(
            ["colima", "--profile", profile, "ssh", "--", "sudo", "fstrim", "-va"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30.0,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def validate_local_docker_storage_headroom(required_gib: int | None) -> None:
    """Require space for a default workspace while preserving the node reserve."""
    subp = _dispatch("subprocess", subprocess)
    context = subp.run(
        ["docker", "context", "show"],
        capture_output=True,
        check=False,
        text=True,
    )
    if context.returncode != 0:
        raise RuntimeError(f"Failed to resolve the active Docker context: {context.stderr.strip()}")
    name = context.stdout.strip()
    if name == "colima":
        profile = "default"
    elif name.startswith("colima-"):
        profile = name.removeprefix("colima-")
    else:
        return

    usage = subp.run(
        ["colima", "--profile", profile, "ssh", "--", "df", "-Pk", "/var/lib/docker"],
        capture_output=True,
        check=False,
        text=True,
    )
    if usage.returncode != 0:
        raise RuntimeError(f"Failed to inspect Colima storage capacity: {usage.stderr.strip()}")
    lines = [line for line in usage.stdout.splitlines() if line.strip()]
    try:
        fields = lines[-1].split()
        capacity = int(fields[1]) * 1024
        available = int(fields[3]) * 1024
    except (IndexError, ValueError) as error:
        raise RuntimeError("Colima returned an unrecognized storage capacity report") from error

    minimum = max(
        (capacity * LOCAL_DOCKER_MINIMUM_FREE_PERCENT + 99) // 100,
        (required_gib or 0) * GIB,
    )
    if available < minimum:
        raise RuntimeError(
            f"Local fleet startup requires {minimum / GIB:.1f} GiB free in Colima's Docker "
            f"filesystem (found {available / GIB:.1f} GiB)."
        )


def validate_local_workspace_storage_headroom(root: Path) -> int | None:
    """Return space needed for a default workspace plus the nodefs reserve."""
    rawfile_path = root / "src/infra/argocd/components/rawfile_localpv/helm/values.yaml"
    if not rawfile_path.is_file():
        return None

    matches = re.findall(
        r"reservedCapacity:\s*[\"\']?([1-9][0-9]*)GiB[\"\']?",
        rawfile_path.read_text(encoding="utf-8"),
    )
    if not matches:
        raise RuntimeError(f"Unable to read the GiB storage reserve from {rawfile_path}")
    reserved_gib = int(matches[0])
    minimum_reserved = (COLIMA_DISK_GIB * LOCAL_NODEFS_EVICTION_PERCENT + 99) // 100
    if reserved_gib < minimum_reserved:
        raise RuntimeError(
            f"Colima disk {COLIMA_DISK_GIB} GiB requires at least {minimum_reserved} GiB reserve "
            f"in {rawfile_path} (found {reserved_gib} GiB)."
        )

    load_deploy = _dispatch("load_local_deployment", load_local_deployment)
    deployment = load_deploy(root)
    workspace_defaults = [
        cluster["workspaces"]["resource_envelope"]["storage_gib"]["default"]
        for cluster in deployment.get("clusters", {}).values()
        if cluster.get("provider") == "floci"
        and (cluster.get("workspaces") or {}).get("enabled") is True
    ]
    if len(workspace_defaults) != 1 or not isinstance(workspace_defaults[0], int):
        raise RuntimeError(
            "Expected exactly one integer local workspace storage default in deployment configuration"
        )
    return reserved_gib + workspace_defaults[0]
