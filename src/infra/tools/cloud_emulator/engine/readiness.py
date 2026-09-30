"""Cluster health, readiness, and TLS certificate synchronization."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import yaml
from infra.tools.cloud_emulator.engine.compose import (
    Fleet,
    compose,
    configuration,
    load_local_deployment,
    run,
)

_T = TypeVar("_T")


def _dispatch(name: str, fallback: _T) -> _T:  # ruff: ignore[non-pep695-generic-function]
    facade = sys.modules.get("infra.tools.cloud_emulator.runtime")
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)  # ty: ignore[unsound-return-statement]
    return fallback


def ensure_headscale_tls(root: Path) -> None:
    tls_dir = root / ".tmp/state/headscale/tls"
    crt = tls_dir / "tls.crt"
    key = tls_dir / "tls.key"
    if crt.is_file() and key.is_file():
        return
    tls_dir.mkdir(parents=True, exist_ok=True)

    cnf = (
        "[req]\n"
        "distinguished_name = req_distinguished_name\n"
        "req_extensions = v3_req\n"
        "prompt = no\n"
        "[req_distinguished_name]\n"
        "CN = headscale.ctrl-eaws-lh1.c.corp.local.internal\n"
        "[v3_req]\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectAltName = @alt_names\n"
        "[alt_names]\n"
        "DNS.1 = headscale.ctrl-eaws-lh1.c.corp.local.internal\n"
        "DNS.2 = headscale.c.corp.local.internal\n"
        "DNS.3 = headscale.corp.local.internal\n"
        "DNS.4 = localhost\n"
        "IP.1 = 172.19.255.23\n"
        "IP.2 = 127.0.0.1\n"
    )
    cnf_path = tls_dir / "openssl.cnf"
    cnf_path.write_text(cnf, encoding="utf-8")

    _dispatch("run", run)(["openssl", "genrsa", "-out", str(key), "2048"])
    csr = tls_dir / "tls.csr"
    _dispatch("run", run)([
        "openssl",
        "req",
        "-new",
        "-key",
        str(key),
        "-out",
        str(csr),
        "-config",
        str(cnf_path),
    ])

    _dispatch("run", run)([
        "openssl",
        "x509",
        "-req",
        "-in",
        str(csr),
        "-signkey",
        str(key),
        "-out",
        str(crt),
        "-days",
        "365",
        "-extfile",
        str(cnf_path),
        "-extensions",
        "v3_req",
    ])
    key.chmod(0o600)
    crt.chmod(0o644)


def _build_floci_openssl_cnf(floci_address: str, registry_address: str) -> str:
    return (
        "[req]\n"
        "distinguished_name = req_distinguished_name\n"
        "req_extensions = v3_req\n"
        "prompt = no\n"
        "[req_distinguished_name]\n"
        "CN = localhost.floci.io\n"
        "[v3_req]\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectAltName = @alt_names\n"
        "[alt_names]\n"
        "DNS.1 = localhost\n"
        "DNS.2 = floci\n"
        "DNS.3 = origin-registry\n"
        "DNS.4 = *.localhost.floci.io\n"
        "DNS.5 = *.dkr.ecr.us-east-1.localhost.floci.io\n"
        "DNS.6 = *.dkr.ecr.us-west-2.localhost.floci.io\n"
        "DNS.7 = *.dkr.ecr.local.localhost.floci.io\n"
        "DNS.8 = *.corp.local.internal\n"
        "DNS.9 = s3.amazonaws.com\n"
        "DNS.10 = *.s3.amazonaws.com\n"
        "DNS.11 = *.s3.us-west-2.amazonaws.com\n"
        "IP.1 = 127.0.0.1\n"
        f"IP.2 = {floci_address}\n"
        f"IP.3 = {registry_address}\n"
    )


def ensure_floci_tls(root: Path) -> None:
    tls_dir = root / ".tmp/state/floci/tls"
    crt = tls_dir / "tls.crt"
    key = tls_dir / "tls.key"
    if crt.is_file() and key.is_file():
        return

    tls_dir.mkdir(parents=True, exist_ok=True)
    cnf_path = tls_dir / "openssl.cnf"
    inventory = _dispatch("load_local_deployment", load_local_deployment)(root)
    services = (inventory.get("network") or inventory["local"]["network"])["services"]
    cnf_path.write_text(
        _build_floci_openssl_cnf(
            str(ipaddress.IPv4Address(services["floci"])),
            str(ipaddress.IPv4Address(services["origin_registry"])),
        ),
        encoding="utf-8",
    )

    _dispatch("run", run)(["openssl", "genrsa", "-out", str(key), "2048"])
    csr = tls_dir / "tls.csr"
    _dispatch("run", run)([
        "openssl",
        "req",
        "-new",
        "-key",
        str(key),
        "-out",
        str(csr),
        "-config",
        str(cnf_path),
    ])

    _dispatch("run", run)([
        "openssl",
        "x509",
        "-req",
        "-in",
        str(csr),
        "-signkey",
        str(key),
        "-out",
        str(crt),
        "-days",
        "365",
        "-extfile",
        str(cnf_path),
        "-extensions",
        "v3_req",
    ])
    key.chmod(0o600)
    crt.chmod(0o644)


def reconcile_tls(root: Path, *, timeout: int = 600) -> None:
    """Export cluster-issued leaves and reload changed local services before Tailnet enrollment."""
    fleet = _dispatch("configuration", configuration)(root)
    inventory = _dispatch("load_local_deployment", load_local_deployment)(root)
    context = control_cluster_record(inventory)
    _dispatch("_reconcile_public_ca", _reconcile_public_ca)(inventory, timeout=timeout)
    hs_file = root / "src/infra/tools/cloud_emulator/stack/headscale/config.yaml"
    if not hs_file.is_file():
        hs_file = root / "src/infra/tools/cloud_emulator/headscale/config.yaml"
    headscale = yaml.safe_load(hs_file.read_text(encoding="utf-8"))
    headscale_hostname = urlsplit(headscale["server_url"]).hostname
    if not headscale_hostname:
        raise RuntimeError("Headscale server URL must have a hostname")
    kubectl = [
        "kubectl",
        "--context",
        context,
        "--request-timeout=10s",
        "-n",
        "cert-manager-system",
    ]
    deadline = _dispatch("time", time).monotonic() + timeout
    services = {"floci": "localhost.floci.io", "headscale": headscale_hostname}
    while True:
        try:
            for service in services:
                _dispatch("run", run)(
                    [
                        *kubectl,
                        "wait",
                        "--for=condition=Ready",
                        f"certificate/local-{service}-tls",
                        "--timeout=10s",
                    ],
                    timeout=15,
                )
            ca = base64.b64decode(
                _dispatch("run", run)([
                    *kubectl,
                    "get",
                    "secret",
                    "cluster-local-ca",
                    "-o",
                    "jsonpath={.data.tls\\.crt}",
                ]).stdout,
                validate=True,
            )
            leaves = {}
            for service in services:
                secret = json.loads(
                    _dispatch("run", run)([
                        *kubectl,
                        "get",
                        "secret",
                        f"local-{service}-tls",
                        "-o",
                        "json",
                    ]).stdout
                )
                leaves[service] = {
                    "tls.crt": base64.b64decode(secret["data"]["tls.crt"], validate=True),
                    "tls.key": base64.b64decode(secret["data"]["tls.key"], validate=True),
                    "ca.crt": ca,
                }
            break
        except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired):
            if _dispatch("time", time).monotonic() >= deadline:
                raise RuntimeError(
                    "Timed out waiting for cluster-issued local runtime TLS certificates"
                ) from None
            _dispatch("time", time).sleep(2)

    # Validate both bundles before changing either service or its on-disk credentials.
    _dispatch("_stage_and_reload_tls", _stage_and_reload_tls)(
        root, fleet, inventory, services, leaves, ca, timeout=timeout
    )
    _dispatch("_reconcile_pod_identity_ca", _reconcile_pod_identity_ca)(
        inventory, ca, timeout=timeout
    )


def _stage_and_reload_tls(
    root: Path,
    fleet: Fleet,
    inventory: dict[str, Any],
    services: dict[str, str],
    leaves: dict[str, dict[str, bytes]],
    ca: bytes,
    *,
    timeout: int,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        for service, hostname in services.items():
            staged = Path(directory) / service
            staged.mkdir(mode=0o700)
            for name, content in leaves[service].items():
                (staged / name).write_bytes(content)
                (staged / name).chmod(0o600)
            _dispatch("_validate_runtime_tls", _validate_runtime_tls)(staged, hostname)
            if service == "floci":
                floci_ip = inventory.get("network", {}).get("services", {}).get(
                    "floci"
                ) or inventory.get("local", {}).get("network", {}).get("services", {}).get("floci")
                if floci_ip:
                    _dispatch("_validate_runtime_tls", _validate_runtime_tls)(staged, floci_ip)

    _dispatch("_reconcile_pod_identity_ca", _reconcile_pod_identity_ca)(
        inventory, ca, timeout=timeout
    )

    for service in services:
        destination = root / ".tmp/state" / service / "tls"
        destination.mkdir(parents=True, exist_ok=True)
        marker = destination / "loaded.sha256"
        fingerprint = hashlib.sha256(b"".join(leaves[service].values())).hexdigest()
        changed = any(
            not (destination / name).is_file() or (destination / name).read_bytes() != content
            for name, content in leaves[service].items()
        )
        reloaded = marker.is_file() and marker.read_text(encoding="utf-8") == fingerprint
        if changed or not reloaded:
            for name, content in leaves[service].items():
                with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                temporary.chmod(0o600 if name == "tls.key" else 0o644)
                temporary.replace(destination / name)
            _dispatch("compose", compose)(
                fleet, "restart", "--timeout", "30", service, timeout=timeout
            )
            marker.write_text(fingerprint, encoding="utf-8")
        (destination / "tls.key").chmod(0o600)
        (destination / "ca.key").unlink(missing_ok=True)


def _reconcile_single_webhook(
    context: str,
    control: str,
    floci_ip: str,
    authority: bytes,
    deadline: float,
) -> None:
    kubectl = ["kubectl", "--context", context, "--request-timeout=10s"]
    annotation = (
        "cert-manager.io/inject-ca-from"
        if context == control
        else "cert-manager.io/inject-ca-from-secret"
    )
    source = "cert-manager-system/" + (
        "local-floci-tls" if context == control else "control-cluster-ca"
    )
    while True:
        try:
            document = json.loads(
                _dispatch("run", run)([
                    *kubectl,
                    "get",
                    "mutatingwebhookconfiguration",
                    "floci-eks-pod-identity",
                    "-o",
                    "json",
                ]).stdout
            )
            client = _pod_identity_client(document, floci_ip)
            metadata = document["metadata"]
            annotations = metadata.get("annotations", {})
            conflicting = {
                "cert-manager.io/inject-ca-from",
                "cert-manager.io/inject-ca-from-secret",
                "cert-manager.io/inject-apiserver-ca",
            } - {annotation}
            if any(key in annotations for key in conflicting):
                raise ValueError("Floci webhook has a different CA injection source")
            if annotations.get(annotation) != source:
                patch = {
                    "metadata": {
                        "resourceVersion": metadata["resourceVersion"],
                        "annotations": {annotation: source},
                    }
                }
                _dispatch("run", run)(
                    [
                        *kubectl,
                        "patch",
                        "mutatingwebhookconfiguration",
                        "floci-eks-pod-identity",
                        "--type=merge",
                        "--patch-file=/dev/stdin",
                    ],
                    stdin=json.dumps(patch),
                )
                if _dispatch("time", time).monotonic() < deadline:
                    continue
            else:
                bundle = base64.b64decode(client.get("caBundle", ""), validate=True)
                if bundle == authority:
                    break
                certificates = re.findall(
                    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                    bundle,
                    re.DOTALL,
                )
                if ssl.PEM_cert_to_DER_cert(authority.decode()) in {
                    ssl.PEM_cert_to_DER_cert(certificate.decode()) for certificate in certificates
                }:
                    _dispatch("run", run)(
                        [
                            *kubectl,
                            "patch",
                            "mutatingwebhookconfiguration",
                            "floci-eks-pod-identity",
                            "--type=json",
                            "--patch-file=/dev/stdin",
                        ],
                        stdin=json.dumps([
                            {
                                "op": "test",
                                "path": "/metadata/resourceVersion",
                                "value": metadata["resourceVersion"],
                            },
                            {
                                "op": "replace",
                                "path": "/webhooks/0/clientConfig/caBundle",
                                "value": base64.b64encode(authority).decode(),
                            },
                        ]),
                    )
                    if _dispatch("time", time).monotonic() < deadline:
                        continue
        except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
        if _dispatch("time", time).monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out waiting for Floci Pod Identity CA injection in {context}"
            )
        _dispatch("time", time).sleep(2)


def _reconcile_pod_identity_ca(
    deployment: dict[str, Any], authority: bytes, *, timeout: int
) -> None:
    """Keep Floci's webhook trust aligned with its externally issued serving certificate."""
    control = control_cluster_record(deployment)
    contexts = cluster_contexts(deployment)
    floci_ip = (
        deployment.get("network", {}).get("services", {}).get("floci")
        or deployment.get("local", {}).get("network", {}).get("services", {}).get("floci")
        or ""
    )
    deadline = _dispatch("time", time).monotonic() + timeout
    for context in contexts:
        _dispatch("_reconcile_single_webhook", _reconcile_single_webhook)(
            context, control, floci_ip, authority, deadline
        )


def _pod_identity_client(document: dict[str, Any], address: str) -> dict[str, Any]:
    webhooks = document["webhooks"]
    if len(webhooks) != 1 or webhooks[0]["name"] != "pod-identity.eks.floci.io":
        raise ValueError("Unexpected Floci webhook identity")
    client = webhooks[0]["clientConfig"]
    endpoint = urlsplit(client["url"])
    if endpoint.scheme != "https" or endpoint.hostname != address:
        raise ValueError("Unexpected Floci webhook endpoint")
    return client


def _validate_runtime_tls(directory: Path, hostname: str) -> None:
    try:
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(directory / "tls.crt"), str(directory / "tls.key"))
        client_context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        completed = set()
        while len(completed) < 2:
            progressed = False
            for peer, outgoing, incoming in (
                (client, client_out, server_in),
                (server, server_out, client_in),
            ):
                if peer not in completed:
                    try:
                        peer.do_handshake()
                        completed.add(peer)
                    except ssl.SSLWantReadError:
                        pass
                if data := outgoing.read():
                    incoming.write(data)
                    progressed = True
            if not progressed and len(completed) < 2:
                raise RuntimeError("Local TLS certificate validation could not complete")
        certificate = client.getpeercert()
        if (
            not certificate
            or ssl.cert_time_to_seconds(str(certificate["notAfter"]))
            <= _dispatch("time", time).time() + 86400
        ):
            raise RuntimeError("Local TLS certificate expires within one day")
    except ssl.SSLError as error:
        reason = (
            error.verify_message
            if isinstance(error, ssl.SSLCertVerificationError)
            else error.reason
        )
        raise RuntimeError(f"Invalid local TLS credentials for {hostname}: {reason}") from None


def control_cluster_record(deployment: dict[str, Any]) -> str:
    clusters = deployment.get("clusters", {})
    for name, cfg in clusters.items():
        if isinstance(cfg, dict) and cfg.get("role") == "ctrl":
            return str(name)
    local = deployment.get("local", deployment)
    if isinstance(local, dict):
        rec = local.get("control", {}).get("record", "")
        if isinstance(rec, str):
            return rec
    return ""


def cluster_contexts(deployment: dict[str, Any]) -> list[str]:
    if "clusters" in deployment:
        clusters = deployment["clusters"]
        ctrl = next((name for name, c in clusters.items() if c.get("role") == "ctrl"), "")
        return [ctrl, *(name for name, c in clusters.items() if c.get("role") == "cell")]
    local = deployment.get("local", deployment)
    return [local["control"]["record"], *(cell["record"] for cell in local["cells"])]


def _reconcile_public_ca(deployment: dict[str, Any], *, timeout: int) -> None:
    control = control_cluster_record(deployment)
    contexts = cluster_contexts(deployment)
    cells = [c for c in contexts if c != control]
    deadline = _dispatch("time", time).monotonic() + timeout
    authorities = {}
    for context in contexts:
        kubectl = [
            "kubectl",
            "--context",
            context,
            "--request-timeout=10s",
            "-n",
            "cert-manager-system",
        ]
        while True:
            try:
                _dispatch("run", run)(
                    [
                        *kubectl,
                        "wait",
                        "--for=condition=Ready",
                        "certificate/cluster-local-ca",
                        "--timeout=10s",
                    ],
                    timeout=15,
                )
                authority = base64.b64decode(
                    _dispatch("run", run)([
                        *kubectl,
                        "get",
                        "secret",
                        "cluster-local-ca",
                        "-o",
                        "jsonpath={.data.tls\\.crt}",
                    ]).stdout,
                    validate=True,
                )
                ssl.create_default_context(cadata=authority.decode("ascii"))
                authorities[context] = authority
                break
            except (RuntimeError, ValueError, ssl.SSLError, subprocess.TimeoutExpired):
                if _dispatch("time", time).monotonic() >= deadline:
                    raise RuntimeError(
                        f"Timed out waiting for the public CA of {context}"
                    ) from None
                _dispatch("time", time).sleep(2)
    for cell in cells:
        _dispatch("_publish_public_ca", _publish_public_ca)(
            cell, "control-cluster-ca", authorities[control]
        )
    if cells:
        _dispatch("_publish_public_ca", _publish_public_ca)(
            control, "cell-cluster-ca", b"\n".join(authorities[cell] for cell in cells)
        )


def _publish_public_ca(context: str, name: str, authority: bytes) -> None:
    kubectl = [
        "kubectl",
        "--context",
        context,
        "--request-timeout=10s",
        "-n",
        "cert-manager-system",
    ]
    current = _dispatch("run", run)([
        *kubectl,
        "get",
        "secret",
        name,
        "--ignore-not-found",
        "-o",
        "json",
    ]).stdout
    data = {"ca.crt": base64.b64encode(authority).decode("ascii")}
    annotations = (
        {"cert-manager.io/allow-direct-injection": "true"} if name == "control-cluster-ca" else {}
    )
    if current.strip():
        secret = json.loads(current)
        if secret.get("metadata", {}).get("ownerReferences") or set(secret.get("data", {})) - {
            "ca.crt"
        }:
            raise RuntimeError(
                f"Refusing to overwrite independently managed public CA secret {context}/{name}"
            )
        if secret.get("data") == data and all(
            secret.get("metadata", {}).get("annotations", {}).get(key) == value
            for key, value in annotations.items()
        ):
            return
        patch: dict[str, Any] = {"data": data, "metadata": {"annotations": annotations}}
        if version := secret.get("metadata", {}).get("resourceVersion"):
            patch["metadata"]["resourceVersion"] = version
        _dispatch("run", run)(
            [*kubectl, "patch", "secret", name, "--type=merge", "--patch-file=/dev/stdin"],
            stdin=json.dumps(patch),
        )
        return
    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": "cert-manager-system", "annotations": annotations},
        "data": data,
    }
    _dispatch("run", run)(
        [*kubectl, "apply", "--server-side", "--field-manager=local-runtime-ca", "-f", "-"],
        stdin=json.dumps(document),
    )


def ensure_headscale_user(fleet: Fleet) -> None:
    if not fleet.headscale_container:
        return
    try:
        users = _dispatch("run", run)([
            "docker",
            "exec",
            fleet.headscale_container,
            "headscale",
            "users",
            "list",
        ]).stdout
        if "local" not in users:
            _dispatch("run", run)([
                "docker",
                "exec",
                fleet.headscale_container,
                "headscale",
                "users",
                "create",
                "local",
            ])
    except Exception:
        pass


def wait_for_cluster(container: str, timeout: int) -> None:
    deadline = _dispatch("time", time).monotonic() + timeout
    while _dispatch("time", time).monotonic() < deadline:
        try:
            _dispatch("run", run)(
                ["docker", "exec", container, "/bin/kubectl", "get", "--raw=/readyz"], timeout=10
            )
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            _dispatch("time", time).sleep(
                min(2, max(0, deadline - _dispatch("time", time).monotonic()))
            )
    raise RuntimeError(f"Retained cluster {container} did not become ready within {timeout}s")


def reconcile_floci_bridge(fleet: Fleet) -> None:
    """Ensure Floci is attached to the default bridge network to reach k3s cluster containers.

    Obsolete: Cluster containers are automatically attached to the VPC bridge by
    Floci (PR #4496), making manual bridge attachments obsolete.
    """
    del fleet


def wait_for_floci_clusters(timeout: int) -> None:
    """Wait for all persisted EKS clusters managed by Floci to report ACTIVE status."""
    deadline = _dispatch("time", time).monotonic() + timeout
    while _dispatch("time", time).monotonic() < deadline:
        try:
            req = urllib.request.Request("http://127.0.0.1:4566/clusters")
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            clusters = data.get("clusters", [])
            if not clusters:
                return
            all_active = True
            for name in clusters:
                req_cluster = urllib.request.Request(f"http://127.0.0.1:4566/clusters/{name}")
                with urllib.request.urlopen(req_cluster, timeout=5) as response:
                    cluster_data = json.loads(response.read().decode("utf-8"))
                if cluster_data.get("cluster", {}).get("status") != "ACTIVE":
                    all_active = False
                    break
            if all_active:
                return
        except urllib.error.URLError as error:
            if isinstance(error.reason, (ConnectionRefusedError, socket.gaierror)):
                return
        except (TimeoutError, json.JSONDecodeError, OSError):
            pass
        _dispatch("time", time).sleep(
            min(1.0, max(0.1, deadline - _dispatch("time", time).monotonic()))
        )
    raise RuntimeError(f"Floci EKS clusters did not become ACTIVE within {timeout}s")


def reconcile_eks_containers(children: list[dict[str, Any]]) -> None:
    """Ensure token webhook endpoints are reconciled on EKS containers."""
    for child in children:
        name = child["Name"].lstrip("/")
        _dispatch("_reconcile_single_eks_container", _reconcile_single_eks_container)(name)


def _reconcile_single_eks_container(container: str, target_ip: str | None = None) -> None:
    del target_ip
    try:
        inspect_res = _dispatch("run", run)(["docker", "inspect", container])
        if inspect_res.returncode != 0:
            return
        details = json.loads(inspect_res.stdout)[0]
        if not details.get("State", {}).get("Running", False):
            return
        _dispatch("_reconcile_token_webhook", _reconcile_token_webhook)(container)
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
        pass


def _reconcile_token_webhook(container: str) -> None:
    webhook_check = _dispatch("run", run)([
        "docker",
        "exec",
        container,
        "sh",
        "-c",
        "test -f /etc/token-webhook.yaml && grep -q '10.1.0.2:4566' /etc/token-webhook.yaml && echo patch || true",
    ])
    if "patch" in webhook_check.stdout:
        _dispatch("run", run)([
            "docker",
            "exec",
            container,
            "sed",
            "-i",
            "s|10.1.0.2:4566|172.19.0.2:4566|g",
            "/etc/token-webhook.yaml",
        ])
