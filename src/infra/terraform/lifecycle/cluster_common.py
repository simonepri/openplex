#!/usr/bin/env python3
"""Provides shared command execution, cluster state checks, and OpenTofu lifecycle utilities."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

DEFAULT_TARGET = "local"
DEFAULT_AVAILABILITY = "standalone"
DEFAULT_UP_TIMEOUT = 600
DEFAULT_DOWN_TIMEOUT = 300
AVAILABILITY_PROFILES = ("standalone", "replicated", "resilient")

CONTEXT_BY_TARGET: dict[str, str] = {
    "local": "ctrl-eaws-lh1",
    "production": "ctrl-aws-usw2",
}


@dataclass(frozen=True)
class PollOptions:
    """Options governing polling and retry backoff."""

    initial_interval: float = 5.0
    backoff_factor: float = 1.2
    max_interval: float = 30.0
    sleep_fn: Callable[[float], None] = time.sleep
    time_fn: Callable[[], float] = time.monotonic


def find_repo_root(start: Path | None = None) -> Path:
    """Resolve the repository root directory."""
    start_path = (
        Path(os.environ["BUILD_WORKSPACE_DIRECTORY"])
        if "BUILD_WORKSPACE_DIRECTORY" in os.environ
        else (start or Path(__file__)).resolve()
    )
    for candidate in [start_path, *start_path.parents]:
        if (candidate / ".git").exists() or (candidate / "MODULE.bazel").exists():
            return candidate
    return Path(__file__).resolve().parents[4]


def get_deployment_dir(target: str, repo_root: Path | None = None) -> Path:
    """Return the absolute path to the OpenTofu deployment directory."""
    root = repo_root or find_repo_root()
    return root / "src" / "infra" / "terraform" / "deployments" / target


def ensure_deployment_dir(target: str, repo_root: Path | None = None) -> Path:
    """Validate and return the OpenTofu deployment directory."""
    target_dir = get_deployment_dir(target, repo_root)
    if not target_dir.is_dir():
        raise FileNotFoundError(f"Deployment directory does not exist: {target_dir}")
    return target_dir


def get_context_for_target(target: str) -> str:
    """Derive the kubectl context name from the deployment target."""
    return CONTEXT_BY_TARGET.get(target.lower(), f"ctrl-{target.lower()}")


def run_tofu_init(target_dir: Path) -> None:
    """Run tofu init in the target deployment directory."""
    plugin_cache = os.environ.get("TF_PLUGIN_CACHE_DIR")
    if plugin_cache:
        Path(plugin_cache).mkdir(parents=True, exist_ok=True)
    cmd = ["tofu", f"-chdir={target_dir}", "init"]
    subprocess.run(cmd, check=True, env=tofu_environment(target_dir))


def run_tofu_apply(target_dir: Path, availability: str) -> None:
    """Run tofu apply in the target deployment directory with fleet availability."""
    cmd = [
        "tofu",
        f"-chdir={target_dir}",
        "apply",
        "-auto-approve",
        f"-var=fleet_availability={availability}",
    ]
    subprocess.run(cmd, check=True, env=tofu_environment(target_dir))


def run_tofu_destroy(target_dir: Path, timeout: float | None = None) -> None:
    """Run tofu destroy in the target deployment directory."""
    cmd = [
        "tofu",
        f"-chdir={target_dir}",
        "destroy",
        "-auto-approve",
    ]
    subprocess.run(cmd, check=True, timeout=timeout, env=tofu_environment(target_dir))


def tofu_environment(target_dir: Path) -> dict[str, str] | None:
    """Use the Docker CLI's active context for local infrastructure providers."""
    if target_dir.name != "local":
        return None
    environment = dict(os.environ)
    if environment.get("DOCKER_HOST") or environment.get("DOCKER_CONTEXT"):
        return environment
    context = subprocess.run(
        ["docker", "context", "show"],
        check=True,
        capture_output=True,
        text=True,
    )
    environment["DOCKER_CONTEXT"] = context.stdout.strip()
    return environment


def query_argocd_status(context: str, *, refresh_pending: bool = False) -> tuple[int, str, str]:
    """Query fleet applications and delivery controllers, including generated children."""
    cmd = [
        "kubectl",
        "--context",
        context,
        "get",
        "applications.argoproj.io,applicationsets.argoproj.io,"
        "warehouses.kargo.akuity.io,stages.kargo.akuity.io",
        "--all-namespaces",
        "--request-timeout=15s",
        "-o",
        "json",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=20)
    except subprocess.TimeoutExpired:
        return 1, "", "Fleet status request timed out"
    if res.returncode:
        return res.returncode, "", res.stderr.strip()
    try:
        items = json.loads(res.stdout)["items"]
    except (json.JSONDecodeError, KeyError):
        return 1, "", "Fleet status response did not contain a Kubernetes resource list"
    if refresh_pending:
        stages = {
            ("Stage", item["metadata"]["namespace"], item["metadata"]["name"]): item
            for item in items
            if item["kind"] == "Stage"
        }
        stalled = sorted(
            item["metadata"]["name"]
            for item in items
            if item["kind"] == "Application"
            and item["metadata"].get("namespace") == "argocd"
            and _resource_pending(item, stages)
        )
        if stalled:
            try:
                refreshed = subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        context,
                        "--request-timeout=15s",
                        "-n",
                        "argocd",
                        "annotate",
                        "applications.argoproj.io",
                        *stalled,
                        "argocd.argoproj.io/refresh=normal",
                        "--overwrite",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
            except subprocess.TimeoutExpired:
                return 1, "", "Application status refresh timed out"
            if refreshed.returncode:
                return (
                    refreshed.returncode,
                    "",
                    ("Application status refresh failed: " + refreshed.stderr.strip()),
                )
    pending = bootstrap_pending(items)
    return 0, ("Pending: " + "; ".join(pending) if pending else "Synced:Healthy"), ""


def bootstrap_pending(items: list[dict[str, Any]]) -> list[str]:
    """Return unmet bootstrap requirements; manual initial promotion may remain pending."""
    resources = {
        (item["kind"], item["metadata"].get("namespace", "argocd"), item["metadata"]["name"]): item
        for item in items
    }
    root = resources.get(("Application", "argocd", "fleet-root"))
    if root is None:
        return ["Application/fleet-root: missing"]
    pending = []
    dispatchers = {
        ("ApplicationSet", child.get("namespace", "argocd"), child["name"])
        for child in root.get("status", {}).get("resources", [])
        if child["kind"] == "ApplicationSet"
    }
    if not dispatchers:
        pending.append("Application/fleet-root: dispatcher inventory not observed")
    for key, item in sorted(resources.items()):
        kind, namespace, name = key
        status = item.get("status", {})
        label = f"{kind}/{namespace}/{name}"
        if kind in {"Application", "ApplicationSet"} and namespace != "argocd":
            continue
        if key in dispatchers and not any(
            child["kind"] == "Application" for child in status.get("resources", [])
        ):
            pending.append(f"{label}: generated application inventory not observed")
        for reason in _resource_pending(item, resources):
            pending.append(f"{label}: {reason}")
        for child in status.get("resources", []):
            child_key = (child["kind"], child.get("namespace", namespace), child["name"])
            if (
                child["kind"] in {"Application", "ApplicationSet", "Warehouse", "Stage"}
                and child_key not in resources
            ):
                pending.append(f"{label}: missing {child['kind']}/{child['name']}")
    return pending


def _resource_pending(
    item: dict[str, Any], resources: dict[tuple[str, str, str], dict[str, Any]]
) -> list[str]:
    status = item.get("status", {})
    kind = item["kind"]
    if kind == "Application":
        stage_ref = (
            item["metadata"].get("annotations", {}).get("kargo.akuity.io/authorized-stage", "")
        )
        stage_namespace, _, stage_name = stage_ref.partition(":")
        stage = resources.get(("Stage", stage_namespace, stage_name))
        sync = status.get("sync", {}).get("status", "Unknown")
        health = status.get("health", {}).get("status", "Unknown")
        awaiting_promotion = (
            stage is not None
            and _awaiting_manual_promotion(stage)
            and (sync, health) == ("OutOfSync", "Missing")
            and status.get("operationState", {}).get("phase")
            not in {"Error", "Failed", "Running", "Terminating"}
            and not any(
                condition.get("type", "").endswith("Error")
                for condition in status.get("conditions", [])
            )
        )
        pending = (
            []
            if (sync, health) == ("Synced", "Healthy") or awaiting_promotion
            else [f"{sync}:{health}"]
        )
        if status.get("operationState", {}).get("phase") in {"Running", "Terminating"}:
            pending.append("sync operation running")
        return pending
    if kind == "ApplicationSet":
        conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
        if (
            conditions.get("ParametersGenerated") != "True"
            or conditions.get("ResourcesUpToDate") != "True"
            or conditions.get("ErrorOccurred") == "True"
        ):
            return ["application generation incomplete"]
        return []
    if kind == "Stage" and _awaiting_manual_promotion(item):
        return []
    conditions = {c["type"]: c for c in status.get("conditions", [])}
    for requirement in ("Ready", "Healthy"):
        condition = conditions.get(requirement, {})
        if condition.get("status") != "True" or condition.get("observedGeneration") != item[
            "metadata"
        ].get("generation"):
            return [f"{requirement}={condition.get('reason', 'Unknown')}"]
    return []


def _awaiting_manual_promotion(stage: dict[str, Any]) -> bool:
    status = stage.get("status", {})
    generation = stage["metadata"].get("generation")
    return (
        generation is not None
        and status.get("autoPromotionEnabled") is False
        and not status.get("freightHistory")
        and any(
            c.get("type") == "Ready"
            and c.get("reason") == "NoFreight"
            and c.get("status") == "False"
            and c.get("observedGeneration") == generation
            for c in status.get("conditions", [])
        )
    )


def wait_for_argocd_sync(
    context: str,
    timeout: float = float(DEFAULT_UP_TIMEOUT),
    options: PollOptions | None = None,
) -> bool:
    """Wait until fleet bootstrap and automatic delivery are healthy or timeout expires."""
    opts = options or PollOptions()
    sleep_fn = opts.sleep_fn
    time_fn = opts.time_fn
    interval = opts.initial_interval
    backoff_factor = opts.backoff_factor
    max_interval = opts.max_interval

    start_time = time_fn()
    last_status = "Pending"
    next_pending_refresh = 0.0

    while True:
        elapsed = time_fn() - start_time
        if elapsed >= timeout:
            raise TimeoutError(
                f"Timed out after {timeout:.0f}s waiting for fleet bootstrap on context '{context}'. "
                f"Last observed status: {last_status}"
            )

        refresh_pending = elapsed >= next_pending_refresh
        if refresh_pending:
            next_pending_refresh = elapsed + 30.0
        code, stdout, stderr = query_argocd_status(context, refresh_pending=refresh_pending)
        if code == 0:
            last_status = stdout
            if last_status == "Synced:Healthy":
                return True
        else:
            err_line = stderr.splitlines()[-1] if stderr else f"exit code {code}"
            last_status = f"Error ({err_line})"

        remaining = timeout - (time_fn() - start_time)
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out after {timeout:.0f}s waiting for fleet bootstrap on context '{context}'. "
                f"Last observed status: {last_status}"
            )

        sleep_duration = min(interval, remaining)
        sleep_fn(sleep_duration)
        interval = min(interval * backoff_factor, max_interval)


def get_public_domain(repo_root: Path | None = None) -> str:
    """Read public_domain from deployment.yaml."""
    root = repo_root or find_repo_root()
    deployment_path = (
        root / "src" / "infra" / "terraform" / "deployments" / "local" / "deployment.yaml"
    )
    if deployment_path.is_file():
        content = deployment_path.read_text(encoding="utf-8")
        match = re.search(r"^\s*public_domain:\s*([^\s#]+)", content, re.MULTILINE)
        if match:
            return str(match.group(1)).strip()
    return "local.internal"  # nosemgrep: repository.lint.forbidden-domain-patterns


def get_intranet_domain(repo_root: Path | None = None) -> str:
    """Read intranet_domain from deployment.yaml or derive from public_domain."""
    root = repo_root or find_repo_root()
    deployment_path = (
        root / "src" / "infra" / "terraform" / "deployments" / "local" / "deployment.yaml"
    )
    if deployment_path.is_file():
        content = deployment_path.read_text(encoding="utf-8")
        match = re.search(r"^\s*intranet_domain:\s*([^\s#]+)", content, re.MULTILINE)
        if match:
            return str(match.group(1)).strip()
    return f"corp.{get_public_domain(repo_root)}"


def get_cluster_domain(repo_root: Path | None = None) -> str:
    """Read cluster_domain from deployment.yaml or derive from intranet_domain."""
    root = repo_root or find_repo_root()
    deployment_path = (
        root / "src" / "infra" / "terraform" / "deployments" / "local" / "deployment.yaml"
    )
    if deployment_path.is_file():
        content = deployment_path.read_text(encoding="utf-8")
        match = re.search(r"^\s*cluster_domain:\s*([^\s#]+)", content, re.MULTILINE)
        if match:
            return str(match.group(1)).strip()
    return f"c.{get_intranet_domain(repo_root)}"
