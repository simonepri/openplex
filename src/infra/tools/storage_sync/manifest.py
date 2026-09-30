"""Generates Kubernetes Job manifests for cell-local and cross-cell storage synchronization."""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from src.infra.tools.storage_sync.topology import NAME_RE, StorageTopology, validate_uri

if TYPE_CHECKING:
    import argparse

IMAGE = (
    "rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5"
)

MAX_DNS_LABEL_LENGTH = 63
TEMPLATE_PATH = Path(__file__).with_name("job.yaml.tmpl")


def required_name(label: str, value: str | None) -> str:
    if value is None or len(value) > MAX_DNS_LABEL_LENGTH or NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a DNS label")
    return value


def _endpoint_cell(uri: str, topology: StorageTopology | None) -> str:
    if topology is not None:
        try:
            return topology.resolve(uri).cell
        except ValueError:
            pass
    for prefix in ("s3://", "gs://", "gcs://"):
        if uri.startswith(prefix):
            return uri.removeprefix(prefix).split("/", 1)[0]
    return ""


def _requires_aws(uri: str, cell: str) -> bool:
    if uri.startswith(("gs://", "gcs://")):
        return False
    return not cell.startswith("gcp")


def _requires_gcp(uri: str, cell: str) -> bool:
    if uri.startswith(("gs://", "gcs://")):
        return True
    return cell.startswith("gcp")


def _build_sync_command(
    options: argparse.Namespace,
    paths: tuple[str, str],
    name: str,
    timestamp: str,
) -> list[str]:
    src_path, dst_path = paths
    if options.approve:
        requester = options.requester or ""
        copy_script = (
            "set -eu\n"
            "if [ -f /etc/storage-sync-ca/ca.crt ]; then\n"
            "  export RCLONE_CA_CERT=/etc/storage-sync-ca/ca.crt\n"
            "fi\n"
            'src="$1"; dst="$2"; src_uri="$3"; req="$4"; job="$5"; ts="$6"\n'
            "rclone copy --metadata "
            '--metadata-set "sync-source=$src_uri" '
            '--metadata-set "sync-requester=$req" '
            '--metadata-set "sync-job=$job" '
            '--metadata-set "sync-timestamp=$ts" '
            '-- "$src" "$dst"\n'
            'rclone check --one-way --size-only -- "$src" "$dst"\n'
            'rclone check --download --one-way -- "$src" "$dst"\n'
        )
        return [
            "sh",
            "-c",
            copy_script,
            "storage-sync",
            src_path,
            dst_path,
            options.source,
            requester,
            name,
            timestamp,
        ]

    dry_run_script = (
        "set -eu\n"
        "if [ -f /etc/storage-sync-ca/ca.crt ]; then\n"
        "  export RCLONE_CA_CERT=/etc/storage-sync-ca/ca.crt\n"
        "fi\n"
        'exec rclone size --json -- "$1"\n'
    )
    return ["sh", "-c", dry_run_script, "storage-sync", src_path]


def _inject_provider_credentials(
    pod_spec: dict[str, Any],
    options: argparse.Namespace,
    topology: StorageTopology | None,
) -> None:
    container = pod_spec["containers"][0]
    src_cell = _endpoint_cell(options.source, topology)
    dst_cell = _endpoint_cell(options.target, topology)
    need_aws = _requires_aws(options.source, src_cell) or _requires_aws(options.target, dst_cell)
    need_gcp = _requires_gcp(options.source, src_cell) or _requires_gcp(options.target, dst_cell)

    if need_aws:
        container["env"].insert(
            0,
            {"name": "AWS_SHARED_CREDENTIALS_FILE", "value": "/var/run/cluster/s3/credentials"},
        )
        container["env"].insert(0, {"name": "AWS_PROFILE", "value": "team-s3"})
        container["volumeMounts"].append({
            "name": "team-s3",
            "mountPath": "/var/run/cluster/s3",
            "readOnly": True,
        })
        pod_spec["volumes"].append({
            "name": "team-s3",
            "secret": {
                "secretName": "team-s3",
                "items": [{"key": "credentials", "path": "credentials"}],
            },
        })

    if need_gcp:
        container["env"].append({
            "name": "GOOGLE_APPLICATION_CREDENTIALS",
            "value": "/var/run/cluster/gcs/credentials.json",
        })
        container["volumeMounts"].append({
            "name": "team-gcs",
            "mountPath": "/var/run/cluster/gcs",
            "readOnly": True,
        })
        pod_spec["volumes"].append({
            "name": "team-gcs",
            "secret": {
                "secretName": "team-gcs",
                "items": [{"key": "credentials.json", "path": "credentials.json"}],
            },
        })


def job_manifest(
    options: argparse.Namespace,
    name: str,
    timestamp: str,
    topology: StorageTopology | None = None,
) -> dict[str, Any]:
    team = required_name("team", options.team)
    namespace = required_name("namespace", options.namespace)
    if not namespace.startswith(f"team-{team}-"):
        raise ValueError(f"namespace must belong to team {team!r}")
    requester = options.requester or ""
    if not re.fullmatch(r"[-._@a-zA-Z0-9]{1,128}", requester):
        raise ValueError("requester contains unsupported characters")
    validate_uri(options.source, team)
    validate_uri(options.target, team)
    if options.source == options.target:
        raise ValueError("source and target must differ")

    if topology is not None:
        src_res = topology.resolve(options.source)
        dst_res = topology.resolve(options.target)
        src_path = src_res.remote_path
        dst_path = dst_res.remote_path
    else:
        src_path = "$SRC_PATH"
        dst_path = "$DST_PATH"

    command = _build_sync_command(options, (src_path, dst_path), name, timestamp)

    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = string.Template(template_text).substitute(
        NAME=name,
        NAMESPACE=namespace,
        AVAILABILITY=options.availability,
        LATENCY=options.latency,
        SYNC_APPROVED=str(options.approve).lower(),
        SYNC_REQUESTER=requester,
        SYNC_SOURCE_URI=options.source,
        SYNC_TARGET_URI=options.target,
        SYNC_TIMESTAMP=timestamp,
    )
    manifest: dict[str, Any] = yaml.safe_load(rendered)

    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    container["command"] = command

    _inject_provider_credentials(pod_spec, options, topology)

    return manifest
