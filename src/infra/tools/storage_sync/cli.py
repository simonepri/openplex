#!/usr/bin/env python3
"""Submits and inspects cell-local and cross-cell storage synchronization jobs."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import secrets
import subprocess

from src.infra.tools.storage_sync.manifest import job_manifest, required_name
from src.infra.tools.storage_sync.topology import StorageTopology


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit a dry-run storage sync; add --approve to copy after the dry-run."
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--availability", required=True, choices=("ha", "ma", "wa", "be"))
    parser.add_argument("--latency", required=True, choices=("ls", "lt"))
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--requester", default=os.getenv("WORKSPACE_USERNAME") or os.getenv("USER"))
    parser.add_argument("--team", default=os.getenv("WORKSPACE_TEAM"))
    parser.add_argument("--namespace")
    parser.add_argument("--cell", default=os.getenv("WORKSPACE_CELL"))
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--follow", action="store_true")
    return parser.parse_args()


def kubectl(args: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(args, input=input_text, text=True, check=True, capture_output=True)
    return result.stdout.strip()


def fetch_topology(kubectl_cmd: str, namespace: str) -> StorageTopology | None:
    try:
        raw = kubectl([
            kubectl_cmd,
            "get",
            "configmap",
            "storage-sync-topology",
            "--namespace",
            namespace,
            "--output=json",
        ])
        cm = json.loads(raw)
        return StorageTopology.from_dict(cm.get("data", {}))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _run(options: argparse.Namespace) -> None:
    required_name("team", options.team)
    if options.namespace is None:
        options.namespace = kubectl([
            options.kubectl,
            "config",
            "view",
            "--minify",
            "--output=jsonpath={..namespace}",
        ])
    namespace = required_name("namespace", options.namespace)
    cell = required_name("cell", options.cell)
    if not cell.startswith("cell-"):
        raise ValueError("cell must name a worker cell")
    context = kubectl([options.kubectl, "config", "current-context"])
    if context != cell:
        raise ValueError(f"kubectl context {context!r} does not match cell {cell!r}")
    allowed = kubectl([
        options.kubectl,
        "auth",
        "can-i",
        "create",
        "jobs.batch",
        "--namespace",
        namespace,
    ])
    if allowed != "yes":
        raise ValueError(f"current identity cannot create Jobs in {namespace!r}")
    queue = options.availability
    kubectl([
        options.kubectl,
        "get",
        "localqueue.kueue.x-k8s.io",
        queue,
        "--namespace",
        namespace,
        "--output=name",
    ])
    now = (
        datetime.datetime
        .now(datetime.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    name = f"storage-sync-{datetime.datetime.now(datetime.UTC):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
    topology = fetch_topology(options.kubectl, namespace)
    if topology is None:
        raise ValueError(
            f"failed to fetch storage-sync-topology ConfigMap from namespace {namespace!r}"
        )
    manifest = job_manifest(options, name, now, topology=topology)
    created = kubectl(
        [options.kubectl, "create", "--filename=-", "--output=name"],
        input_text=json.dumps(manifest),
    )
    if options.follow:
        subprocess.run(
            [
                options.kubectl,
                "wait",
                "--for=condition=complete",
                "--timeout=24h",
                created,
                "--namespace",
                namespace,
            ],
            check=True,
        )
        subprocess.run(
            [options.kubectl, "logs", "--namespace", namespace, f"job/{name}"], check=True
        )


def main() -> int:
    options = parse_args()
    try:
        _run(options)
    except (ValueError, subprocess.CalledProcessError):
        return 2
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
