"""Reconciles tagged router enrollment keys for the external local Headscale runtime."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from infra.tools.cloud_emulator import runtime

# LINT.IfChange(router-auth-key-contract)
ROUTER_TAGS = frozenset(("tag:k8s-egress", "tag:subnet-router"))
KEY_ID = "headscale-preauth-key-id"
KEY_EXPIRY = "headscale-preauth-expiration-epoch"
KEY_DIGEST = "headscale-preauth-key-sha256"
KEY_LIFETIME = "720h"
ROTATION_LEAD_SECONDS = 86400
AUTH_KEY = re.compile(r"hskey-auth-([A-Za-z0-9_-]{12})-[A-Za-z0-9_-]{64}")
# LINT.ThenChange(//src/infra/tools/headscale_key_producer/main.go:router-auth-key-contract)


def reconcile(root: Path, *, timeout: int = 600) -> None:
    """Publish a reusable router key before GitOps waits for Tailnet-dependent workloads."""
    manifest = runtime.load_local_deployment(root)
    control = runtime.control_cluster_record(manifest)
    contexts = runtime.cluster_contexts(manifest)
    record_name = f"headscale-preauth-{control}"
    fleet = runtime.configuration(root)
    if not fleet.headscale_container:
        raise RuntimeError("Local router enrollment requires the managed Headscale service")
    command = ["docker", "exec", fleet.headscale_container, "headscale", "preauthkeys"]
    deadline = time.monotonic() + timeout
    records = {context: _wait_for_record(context, record_name, deadline) for context in contexts}
    keys = json.loads(_run([*command, "list", "--output", "json"]))
    if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
        raise RuntimeError("Headscale returned an invalid router enrollment key list")
    cutoff = int(time.time()) + ROTATION_LEAD_SECONDS
    selected = next(
        (key for record in records.values() if (key := _current_key(record, keys, cutoff))),
        None,
    )
    if selected is None:
        selected = json.loads(
            _run([
                *command,
                "create",
                "--reusable",
                "--expiration",
                KEY_LIFETIME,
                "--tags",
                ",".join(sorted(ROUTER_TAGS)),
                "--output",
                "json",
            ])
        )
        if not _valid_key(selected, cutoff) or not AUTH_KEY.fullmatch(selected.get("key", "")):
            raise RuntimeError("Headscale returned an invalid router enrollment key")

    auth_key = selected["key"]
    annotations = {
        KEY_ID: str(selected["id"]),
        KEY_EXPIRY: str(selected["expiration"]["seconds"]),
        KEY_DIGEST: hashlib.sha256(auth_key.encode()).hexdigest(),
    }
    encoded = base64.b64encode(auth_key.encode()).decode("ascii")
    for context in contexts:
        while True:
            record = _wait_for_record(context, record_name, deadline)
            metadata = record.get("metadata", {})
            if record.get("data", {}).get("authkey") == encoded and all(
                metadata.get("annotations", {}).get(key) == value
                for key, value in annotations.items()
            ):
                break
            patch = {
                "metadata": {
                    "resourceVersion": metadata["resourceVersion"],
                    "annotations": annotations,
                },
                "data": {"authkey": encoded},
            }
            try:
                _run(
                    [
                        *_kubectl(context),
                        "patch",
                        "secret",
                        record_name,
                        "--type=merge",
                        "--patch-file=/dev/stdin",
                    ],
                    stdin=json.dumps(patch),
                )
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Timed out publishing the local router enrollment key"
                    ) from None
                time.sleep(2)
    for context in contexts:
        _wait_for_target(context, encoded, deadline)


def _valid_key(key: dict[str, Any], cutoff: int) -> bool:
    try:
        return (
            int(key["id"]) > 0
            and key["reusable"] is True
            and len(key["acl_tags"]) == len(ROUTER_TAGS)
            and set(key["acl_tags"]) == ROUTER_TAGS
            and int(key["expiration"]["seconds"]) >= cutoff
        )
    except (KeyError, TypeError, ValueError):
        return False


def _current_key(
    record: dict[str, Any], keys: list[dict[str, Any]], cutoff: int
) -> dict[str, Any] | None:
    try:
        raw = base64.b64decode(record["data"]["authkey"], validate=True)
        auth_key = raw.decode("ascii")
        match = AUTH_KEY.fullmatch(auth_key)
        annotations = record["metadata"]["annotations"]
        if not match or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), annotations[KEY_DIGEST]
        ):
            return None
        for key in keys:
            if (
                _valid_key(key, cutoff)
                and str(key["id"]) == annotations[KEY_ID]
                and str(key["expiration"]["seconds"]) == annotations[KEY_EXPIRY]
                and hmac.compare_digest(key["key"], f"hskey-auth-{match[1]}-***")
            ):
                return {**key, "key": auth_key}
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _wait_for_record(context: str, name: str, deadline: float) -> dict[str, Any]:
    while True:
        try:
            result = _run([
                *_kubectl(context),
                "get",
                "secret",
                name,
                "--ignore-not-found",
                "-o",
                "json",
            ])
            if result.strip():
                return json.loads(result)
        except RuntimeError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for the router enrollment record in {context}")
        time.sleep(2)


def _wait_for_target(context: str, encoded: str, deadline: float) -> None:
    kubectl = _kubectl(context, namespace="tailscale-system")
    refreshed = False
    while True:
        try:
            target = json.loads(
                _run([
                    *kubectl,
                    "get",
                    "secret",
                    "headscale-preauth",
                    "--ignore-not-found",
                    "-o",
                    "json",
                ])
                or "{}"
            )
            if hmac.compare_digest(target.get("data", {}).get("authkey", ""), encoded):
                return
            if (
                not refreshed
                and _run([
                    *kubectl,
                    "get",
                    "externalsecret",
                    "headscale-preauth",
                    "--ignore-not-found",
                    "-o",
                    "name",
                ]).strip()
            ):
                _run([
                    *kubectl,
                    "annotate",
                    "externalsecret",
                    "headscale-preauth",
                    f"force-sync={time.time_ns()}",
                    "--overwrite",
                ])
                refreshed = True
        except RuntimeError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for the router enrollment target in {context}")
        time.sleep(2)


def _kubectl(context: str, *, namespace: str = "secret-records") -> list[str]:
    return ["kubectl", "--context", context, "--request-timeout=10s", "-n", namespace]


def _run(arguments: list[str], *, stdin: str | None = None) -> str:
    try:
        return runtime.run(arguments, stdin=stdin, timeout=20).stdout
    except (RuntimeError, subprocess.SubprocessError):
        # CLI failures can contain Secret data; only the operation's failure crosses this boundary.
        raise RuntimeError("Local router enrollment command failed") from None
