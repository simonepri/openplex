"""Projects verified local cluster credentials into a private repository kubeconfig."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml
from infra.tools.cloud_emulator.engine import compose


def project(root: Path) -> Path:
    """Publish current node credentials and activate their mapped host contexts."""
    root = root.resolve()
    deployment = root / "src/infra/terraform/deployments/local/deployment.yaml"
    clusters = yaml.safe_load(deployment.read_text(encoding="utf-8"))["clusters"]
    fleet = compose.configuration(root)
    containers = compose.owned_containers(fleet)
    document: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [],
        "contexts": [],
        "users": [],
    }
    for name, record in clusters.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name)
            or name == "local"
        ):
            raise RuntimeError("Local cluster name cannot be used as a kubeconfig filename")
        names = {f"/floci-{fleet.namespace}-eks-{name}", f"/floci-aws-{fleet.namespace}-eks-{name}"}
        matches = [container for container in containers if container["Name"] in names]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one local node for cluster {name}")
        container = matches[0]
        bindings = container["NetworkSettings"]["Ports"].get("6443/tcp") or []
        ports = {binding["HostPort"] for binding in bindings}
        if len(ports) != 1:
            raise RuntimeError(f"Expected one published API port for cluster {name}")
        port = int(ports.pop())
        result = subprocess.run(
            [
                "docker",
                "exec",
                container["Id"],
                "/bin/kubectl",
                "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
                "config",
                "view",
                "--raw",
                "--flatten",
                "--minify",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError(f"Could not read local credentials for cluster {name}")
        cluster, user = _credentials(result.stdout)
        cluster["server"] = f"https://127.0.0.1:{port}"
        document["clusters"].append({"name": name, "cluster": cluster})
        document["users"].append({"name": name, "user": user})
        document["contexts"].append({"name": name, "context": {"cluster": name, "user": name}})
        if record["role"] == "ctrl":
            document["current-context"] = name
    destination = root / ".tmp/kubeconfigs/local.yaml"
    directory = destination.parent
    if not directory.resolve().is_relative_to(root):
        raise RuntimeError("Local kubeconfig directory must remain inside the repository")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with tempfile.TemporaryDirectory(dir=directory) as temporary_directory:
        temporary = Path(temporary_directory) / "config"
        temporary.write_text(json.dumps(document), encoding="utf-8")
        temporary.chmod(0o600)
        for context in document["contexts"]:
            result = subprocess.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(temporary),
                    "--context",
                    context["name"],
                    "--request-timeout=10s",
                    "get",
                    "--raw=/readyz",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=15,
            )
            if result.returncode:
                raise RuntimeError(f"Local API TLS/readiness check failed for {context['name']}")
        staged = [(temporary, destination)]
        for cluster, context, user in zip(
            document["clusters"], document["contexts"], document["users"], strict=True
        ):
            name = context["name"]
            minified = {
                **document,
                "clusters": [cluster],
                "contexts": [context],
                "users": [user],
                "current-context": name,
            }
            path = Path(temporary_directory) / f"{name}.yaml"
            path.write_text(json.dumps(minified), encoding="utf-8")
            path.chmod(0o600)
            staged.append((path, directory / path.name))
        for path, target in staged:
            path.replace(target)
    activate(root)
    return destination


def activate(root: Path) -> None:
    """Prepend persisted local contexts while retaining the user's other kubeconfigs."""
    path = root.resolve() / ".tmp/kubeconfigs/local.yaml"
    if not path.is_file():
        return
    existing = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube/config")
    paths = [str(path), *existing.split(os.pathsep)]
    os.environ["KUBECONFIG"] = os.pathsep.join(dict.fromkeys(item for item in paths if item))


def _credentials(text: str) -> tuple[dict[str, str], dict[str, str]]:
    try:
        source = yaml.safe_load(text)
        cluster = source["clusters"][0]["cluster"]
        user = source["users"][0]["user"]
        ca = cluster["certificate-authority-data"]
        certificate = user["client-certificate-data"]
        key = user["client-key-data"]
        context = ssl.create_default_context(cadata=base64.b64decode(ca, validate=True).decode())
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(base64.b64decode(certificate, validate=True))
            stream.write(b"\n")
            stream.write(base64.b64decode(key, validate=True))
            stream.flush()
            context.load_cert_chain(stream.name)
    except (KeyError, IndexError, TypeError, ValueError, ssl.SSLError, yaml.YAMLError):
        raise RuntimeError("Local node kubeconfig has missing or invalid TLS credentials") from None
    return {"certificate-authority-data": ca}, {
        "client-certificate-data": certificate,
        "client-key-data": key,
    }
