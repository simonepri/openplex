"""Container lifecycle, network bridge attachment, and compose services."""

from __future__ import annotations

import ipaddress
import json
import operator
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml

_T = TypeVar("_T")


def _dispatch(name: str, fallback: _T) -> _T:  # ruff: ignore[non-pep695-generic-function]
    facade = sys.modules.get("infra.tools.cloud_emulator.runtime")
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)  # ty: ignore[unsound-return-statement]
    return fallback


_T2 = TypeVar("_T2")


def _runtime_symbol(name: str) -> _T2:
    facade = sys.modules.get("infra.tools.cloud_emulator.runtime")
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)  # ty: ignore[unsound-return-statement]
    raise RuntimeError(f"Runtime symbol {name} is unavailable")


GIT_DAEMON_COMMAND = (
    "-c",
    "safe.directory=*",
    "daemon",
    "--base-path=/srv/git",
    "--export-all",
    "--informative-errors",
    "--listen=0.0.0.0",
    "--port=9418",
    "--reuseaddr",
    "/srv/git",
)
GIT_DAEMON_DOCKERFILE = """# Builds the read-only Git daemon used by the local GitOps runtime.

FROM docker.io/library/alpine:3.22.2@sha256:4b7ce07002c69e8f3d704a9c5d6fd3053be500b7f1c69fc0d80990c2ad8dd412

RUN apk add --no-cache git=2.49.1-r0 git-daemon=2.49.1-r0
RUN apk add --no-cache busybox-extras=1.37.0-r20
RUN mkdir -p /srv/http/cgi-bin && touch /srv/http/index.html && printf '%s\\n' \\
    '#!/bin/sh' \\
    'case "$QUERY_STRING:$PATH_INFO" in *service=git-receive-pack*|*/git-receive-pack) printf "Status: 403 Forbidden\\r\\n\\r\\n"; exit 0 ;; esac' \\
    'export GIT_PROJECT_ROOT=/srv/git GIT_HTTP_EXPORT_ALL=1' \\
    'exec git -c safe.directory=/srv/git/openplex.git -c http.receivepack=false http-backend' \\
    > /srv/http/cgi-bin/git && chmod 0555 /srv/http/cgi-bin/git

USER 65534:65534

ENTRYPOINT ["git"]
CMD ["daemon"]
"""
GIT_DAEMON_LEGACY_IMAGES = frozenset(("local/git-daemon", "local/git-daemon:latest"))
GIT_DAEMON_USER = "65534:65534"


@dataclass(frozen=True)
class Fleet:
    """Persistent runtime identity read from the rendered Compose configuration."""

    compose_file: Path
    project: str
    container: str
    git_address: str
    git_container: str
    git_directory: Path
    git_image: str
    git_repository: str
    namespace: str
    network: str
    volume: str
    git_watcher_container: str = ""
    git_http_container: str = ""
    headscale_address: str = ""
    headscale_container: str = ""
    headscale_image: str = ""
    headscale_volume: str = ""


def run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    include_stdout_on_error: bool = False,
    stdin: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        capture_output=True,
        input=stdin,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        # Container inspections include credentials on stdout; callers opt in only for safe output.
        details = completed.stderr.strip()
        if include_stdout_on_error and completed.stdout.strip():
            details = "\n".join(part for part in (completed.stdout.strip(), details) if part)
        raise RuntimeError(
            f"{' '.join(arguments)} exited with status {completed.returncode}: {details}"
        )
    return completed


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
    return Path(__file__).resolve().parents[3]


def compose(fleet: Fleet, *arguments: str, timeout: int) -> None:
    _dispatch("run", run)(
        ["docker-compose", "--file", str(fleet.compose_file), *arguments], timeout=timeout
    )


def configuration(root: Path) -> Fleet:
    compose_file = root / "src/infra/tools/cloud_emulator/stack/compose.yaml"
    if not compose_file.is_file():
        legacy = root / "src/infra/tools/cloud_emulator/compose.yaml"
        if legacy.is_file():
            compose_file = legacy
    document = json.loads(
        _dispatch("run", run)(
            [
                "docker-compose",
                "--file",
                str(compose_file),
                "config",
                "--format",
                "json",
            ],
            cwd=root,
        ).stdout
    )
    service = document["services"]["floci"]
    git = document["services"]["git"]
    if "build" in git:
        raise RuntimeError("Compose must not build the local Git daemon image")
    git_mounts = git.get("volumes") or []
    if len(git_mounts) != 1 or any((
        git_mounts[0].get("type") != "bind",
        git_mounts[0].get("target") != "/srv/git/openplex.git",
        git_mounts[0].get("read_only") is not True,
        (git_mounts[0].get("bind") or {}).get("create_host_path") is not False,
    )):
        raise RuntimeError("Local Git daemon must use its read-only repository bind mount")
    git_mount = git_mounts[0]
    git_directory = (root / ".git").resolve()
    if not git_directory.is_dir() or Path(git_mount["source"]).resolve() != git_directory:
        raise RuntimeError("Local Git daemon must mount this checkout's .git directory")
    git_address = str(ipaddress.IPv4Address(git["networks"]["default"]["ipv4_address"]))
    deployment_record = load_local_deployment(root)
    network_services = (
        deployment_record.get("network") or deployment_record.get("local", {}).get("network", {})
    ).get("services", {})
    floci_address = str(ipaddress.IPv4Address(service["networks"]["default"]["ipv4_address"]))
    if floci_address != str(ipaddress.IPv4Address(network_services["floci"])):
        raise RuntimeError("Local Floci address differs from the fleet inventory")
    deployment_address = str(ipaddress.IPv4Address(network_services["git"]))
    if git_address != deployment_address:
        raise RuntimeError("Local Git daemon address differs from the fleet inventory")
    headscale = document.get("services", {}).get("headscale")
    headscale_address = ""
    headscale_container = ""
    headscale_image = ""
    headscale_volume = ""
    if headscale is not None:
        headscale_address = str(
            ipaddress.IPv4Address(headscale["networks"]["default"]["ipv4_address"])
        )
        headscale_container = headscale.get("container_name", "")
        headscale_image = headscale.get("image", "")
        headscale_volume = (document.get("volumes", {}).get("headscale_state") or {}).get(
            "name", "openplex-local-headscale-data"
        )
        deployment_services = network_services
        if "headscale" in deployment_services:
            deployment_headscale = str(ipaddress.IPv4Address(deployment_services["headscale"]))
            if headscale_address != deployment_headscale:
                raise RuntimeError("Local Headscale address differs from the fleet inventory")
    git_watcher = document.get("services", {}).get("git-watcher")
    git_watcher_container = git_watcher.get("container_name", "") if git_watcher else ""
    return Fleet(
        compose_file=compose_file,
        project=document["name"],
        container=service["container_name"],
        git_address=git_address,
        git_container=git["container_name"],
        git_directory=git_directory,
        git_image=git["image"],
        git_repository=git_mount["target"],
        git_watcher_container=git_watcher_container,
        git_http_container=(document.get("services", {}).get("git-http") or {}).get(
            "container_name", ""
        ),
        headscale_address=headscale_address,
        headscale_container=headscale_container,
        headscale_image=headscale_image,
        headscale_volume=headscale_volume,
        namespace=service["environment"]["FLOCI_DOCKER_RESOURCE_NAMESPACE"],
        network=document["networks"]["default"]["name"],
        volume=document["volumes"]["state"]["name"],
    )


def load_local_deployment(root: Path) -> dict[str, Any]:
    deployment_path = root / "src/infra/terraform/deployments/local/deployment.yaml"
    if deployment_path.is_file():
        with deployment_path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
            if isinstance(data, dict):
                return data
    raise RuntimeError(f"Deployment manifest {deployment_path} could not be loaded")


def inspect_containers() -> list[dict[str, Any]]:
    names = _dispatch("run", run)([
        "docker",
        "ps",
        "--all",
        "--format",
        "{{.Names}}",
    ]).stdout.splitlines()
    if not names:
        return []
    return json.loads(_dispatch("run", run)(["docker", "inspect", *names]).stdout)


def owned_containers(
    fleet: Fleet, containers: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    inspected = containers if containers is not None else inspect_containers()
    prefixes = (
        f"/floci-{fleet.namespace}-",
        f"/floci-aws-{fleet.namespace}-",
    )
    return [
        item
        for item in inspected
        if item["Name"] == f"/{fleet.container}"
        or any(item["Name"].startswith(prefix) for prefix in prefixes)
    ]


def find_git_daemon(fleet: Fleet, containers: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in containers if item["Name"] == f"/{fleet.git_container}"), None)


def find_headscale_daemon(fleet: Fleet, containers: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not fleet.headscale_container:
        return None
    return next(
        (item for item in containers if item["Name"] == f"/{fleet.headscale_container}"), None
    )


def validate_git_daemon(container: dict[str, Any], fleet: Fleet) -> bool:
    """Validate the named daemon and report whether Compose must adopt it."""
    mount = container.get("Mounts") or []
    if len(mount) != 1 or any((
        mount[0].get("Type") != "bind",
        Path(mount[0].get("Source", "")).resolve() != fleet.git_directory,
        mount[0].get("Destination") != fleet.git_repository,
        mount[0].get("RW") is not False,
    )):
        raise RuntimeError(f"Container /{fleet.git_container} has an unexpected repository mount")

    networks = (container.get("NetworkSettings") or {}).get("Networks", {})
    endpoint = networks.get(fleet.network)
    if (
        set(networks) != {fleet.network}
        or endpoint is None
        or endpoint_address(endpoint) != fleet.git_address
    ):
        raise RuntimeError(f"Container /{fleet.git_container} has an unexpected network endpoint")
    expected_ports = {"9418/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9418"}]}
    host = container.get("HostConfig") or {}
    if host.get("PortBindings") != expected_ports:
        raise RuntimeError(f"Container /{fleet.git_container} has unexpected published ports")
    if (
        host.get("AutoRemove") is not False
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != fleet.network
    ):
        raise RuntimeError(f"Container /{fleet.git_container} has unsafe runtime settings")

    labels = (container.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    if project is not None or service is not None:
        if project == fleet.project and service == "git":
            return False
        raise RuntimeError(f"Container /{fleet.git_container} belongs to another Compose service")
    if labels:
        raise RuntimeError(f"Container /{fleet.git_container} has unrecognized ownership labels")

    config = container.get("Config") or {}
    if (
        config.get("Image") not in GIT_DAEMON_LEGACY_IMAGES
        or config.get("Entrypoint") != ["git"]
        or tuple(config.get("Cmd") or ()) != GIT_DAEMON_COMMAND
        or config.get("User") != GIT_DAEMON_USER
        or (host.get("RestartPolicy") or {}).get("Name") not in {"no", "unless-stopped"}
    ):
        raise RuntimeError(f"Container /{fleet.git_container} is not the known legacy Git daemon")
    return True


def validate_managed_git_daemon(container: dict[str, Any], fleet: Fleet) -> None:
    if validate_git_daemon(container, fleet):
        raise RuntimeError("Local Git daemon was not adopted by Compose")
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    health = (container.get("State") or {}).get("Health") or {}
    if (
        config.get("Image") != fleet.git_image
        or config.get("Entrypoint") != ["git"]
        or config.get("User") != GIT_DAEMON_USER
        or tuple(config.get("Cmd") or ()) != GIT_DAEMON_COMMAND
        or host.get("CapAdd") not in (None, [])
        or host.get("CapDrop") != ["ALL"]
        or host.get("ReadonlyRootfs") is not True
        or (host.get("RestartPolicy") or {}).get("Name") != "unless-stopped"
        or host.get("SecurityOpt") != ["no-new-privileges:true"]
        or not (container.get("State") or {}).get("Running")
        or health.get("Status") != "healthy"
    ):
        raise RuntimeError("Compose did not establish the local Git daemon contract")


def validate_git_address(
    fleet: Fleet, containers: list[dict[str, Any]], git: dict[str, Any] | None
) -> None:
    for container in containers:
        endpoint = (container.get("NetworkSettings") or {}).get("Networks", {}).get(fleet.network)
        if (
            endpoint is not None
            and endpoint_address(endpoint) == fleet.git_address
            and (git is None or container["Id"] != git["Id"])
        ):
            raise RuntimeError(
                f"Reserved Git address {fleet.git_address} is occupied by {container['Name']}"
            )


def validate_git_port(containers: list[dict[str, Any]], git: dict[str, Any] | None) -> None:
    for container in containers:
        if git is not None and container["Id"] == git["Id"]:
            continue
        bindings = ((container.get("HostConfig") or {}).get("PortBindings") or {}).values()
        if any(
            binding.get("HostPort") == "9418"
            and binding.get("HostIp", "") in {"", "0.0.0.0", "127.0.0.1", "::"}
            for ports in bindings
            for binding in (ports or [])
        ):
            raise RuntimeError(f"Local Git port 127.0.0.1:9418 is occupied by {container['Name']}")


def endpoint_address(endpoint: dict[str, Any]) -> str | None:
    value = (endpoint.get("IPAMConfig") or {}).get("IPv4Address") or endpoint.get("IPAddress")
    if not isinstance(value, str):
        return None
    parts = value.split("/", 1)
    assert isinstance(parts[0], str)
    return parts[0]


def retire_legacy_git_daemon(container: dict[str, Any], timeout: int) -> None:
    if container["State"]["Running"]:
        _dispatch("run", run)(
            ["docker", "stop", "--time", str(timeout), container["Id"]], timeout=timeout + 30
        )
    _dispatch("run", run)(["docker", "rm", container["Id"]])


def validate_parent(parent: dict[str, Any], fleet: Fleet) -> None:
    settings = dict(item.split("=", 1) for item in parent["Config"]["Env"])
    if settings.get("FLOCI_DOCKER_RESOURCE_NAMESPACE") != fleet.namespace:
        raise RuntimeError("Existing Floci container belongs to a different fleet")
    volume = next(
        (mount for mount in parent["Mounts"] if mount["Destination"] == "/app/data"), None
    )
    if volume is None or volume["Type"] != "volume" or volume["Name"] != fleet.volume:
        raise RuntimeError("Existing Floci data volume differs from the Compose declaration")
    if fleet.network not in parent["NetworkSettings"]["Networks"]:
        raise RuntimeError("Existing Floci network differs from the Compose declaration")
    if any(
        settings.get(key) != expected
        for key, expected in (
            ("FLOCI_SERVICES_EKS_KEEP_RUNNING_ON_SHUTDOWN", "true"),
            ("FLOCI_SERVICES_ECR_KEEP_RUNNING_ON_SHUTDOWN", "true"),
            ("FLOCI_STORAGE_MODE", "persistent"),
            ("FLOCI_STORAGE_PRUNE_VOLUMES_ON_DELETE", "false"),
        )
    ):
        raise RuntimeError(
            "Existing Floci runtime must retain its services and persistent volumes on shutdown"
        )


def reconcile_registry(root: Path, *, required: bool = True) -> None:
    """Attach the fleet's existing ECR registry at its reserved address and alias."""
    fleet = _dispatch("configuration", configuration)(root)
    names = {
        f"/floci-aws-{fleet.namespace}-ecr-registry",
        f"/floci-{fleet.namespace}-ecr-registry",
    }
    registries = [item for item in owned_containers(fleet) if item["Name"] in names]
    if not registries:
        if required:
            raise RuntimeError(
                f"Floci registry for {fleet.namespace} is missing after foundation reconciliation"
            )
        return
    if len(registries) != 1:
        raise RuntimeError(f"Multiple Floci registries identify fleet {fleet.namespace}")
    registry = registries[0]
    name = registry["Name"]
    labels = registry["Config"].get("Labels") or {}
    if labels.get("floci_namespace") != fleet.namespace or labels.get("io.floci.service") != "ecr":
        raise RuntimeError(f"Container {name} does not identify the fleet's Floci ECR registry")
    local_data = load_local_deployment(root)
    network_services = (
        local_data.get("network") or local_data.get("local", {}).get("network", {})
    ).get("services", {})
    address = str(ipaddress.IPv4Address(network_services["origin_registry"]))
    previous = registry["NetworkSettings"]["Networks"].get(fleet.network)
    if registry_endpoint_matches(previous, address, running=registry["State"]["Running"]):
        return

    network = json.loads(
        _dispatch("run", run)(["docker", "network", "inspect", fleet.network]).stdout
    )[0]
    for identifier, endpoint in (network.get("Containers") or {}).items():
        if identifier != registry["Id"] and endpoint["IPv4Address"].split("/")[0] == address:
            raise RuntimeError(f"Reserved registry address {address} is occupied by {identifier}")
    aliases = sorted(set((previous or {}).get("Aliases") or []) | {"origin-registry"})
    try:
        if previous is not None:
            _dispatch("run", run)([
                "docker",
                "network",
                "disconnect",
                fleet.network,
                registry["Id"],
            ])
        connect_registry(fleet, registry["Id"], address, aliases)
        current = json.loads(_dispatch("run", run)(["docker", "inspect", registry["Id"]]).stdout)[0]
        current_mounts = sorted(current["Mounts"], key=operator.itemgetter("Destination"))
        previous_mounts = sorted(registry["Mounts"], key=operator.itemgetter("Destination"))
        if current["Id"] != registry["Id"] or current_mounts != previous_mounts:
            raise RuntimeError(
                "Registry endpoint reconciliation changed the retained container or mounts"
            )
        if not registry_endpoint_matches(
            current["NetworkSettings"]["Networks"].get(fleet.network),
            address,
            running=current["State"]["Running"],
        ):
            raise RuntimeError("Registry endpoint does not match its reserved address and alias")
    except (RuntimeError, subprocess.TimeoutExpired):
        restore_registry_endpoint(fleet, registry["Id"], previous)
        raise


def restore_registry_endpoint(
    fleet: Fleet, identifier: str, previous: dict[str, Any] | None
) -> None:
    current = json.loads(_dispatch("run", run)(["docker", "inspect", identifier]).stdout)[0]
    if current["NetworkSettings"]["Networks"].get(fleet.network) == previous:
        return
    if fleet.network in current["NetworkSettings"]["Networks"]:
        _dispatch("run", run)(["docker", "network", "disconnect", fleet.network, identifier])
    if previous is not None:
        # Preserve the last assigned address when restoring a dynamic attachment.
        previous_address = (
            (previous.get("IPAMConfig") or {}).get("IPv4Address")
            or previous.get("IPAddress")
            or None
        )
        connect_registry(fleet, identifier, previous_address, previous.get("Aliases") or [])


def registry_endpoint_matches(
    endpoint: dict[str, Any] | None, address: str, *, running: bool
) -> bool:
    return bool(
        endpoint is not None
        and (not running or endpoint.get("IPAddress") == address)
        and (endpoint.get("IPAMConfig") or {}).get("IPv4Address") == address
        and "origin-registry" in (endpoint.get("Aliases") or [])
    )


def connect_registry(
    fleet: Fleet, identifier: str, address: str | None, aliases: list[str]
) -> None:
    arguments = ["docker", "network", "connect"]
    if address is not None:
        arguments.extend(["--ip", address])
    for alias in aliases:
        arguments.extend(["--alias", alias])
    _dispatch("run", run)([*arguments, fleet.network, identifier])


def _prepare_runtime_images(fleet: Fleet, root: Path, timeout: int) -> None:
    _runtime_symbol("ensure_floci_tls")(root)
    _dispatch("compose", compose)(fleet, "pull", "--policy", "missing", "floci", timeout=timeout)
    if fleet.git_watcher_container:
        _dispatch("compose", compose)(
            fleet, "pull", "--policy", "missing", "git-watcher", timeout=timeout
        )
    if fleet.headscale_container:
        _runtime_symbol("ensure_headscale_tls")(root)
        _dispatch("compose", compose)(
            fleet, "pull", "--policy", "missing", "headscale", timeout=timeout
        )
    _dispatch("run", run)(
        [
            "docker",
            "build",
            "--tag",
            fleet.git_image,
            "-",
        ],
        include_stdout_on_error=True,
        stdin=GIT_DAEMON_DOCKERFILE,
        timeout=timeout,
    )
    _dispatch("_ensure_k3s_runtime_image", _ensure_k3s_runtime_image)(root, timeout=timeout)


def _pinned_k3s_runtime_tag(root: Path) -> str:
    images_toml = root / "src/third_party/k3s-io/k3s/images.toml"
    if not images_toml.is_file():
        return ""
    for line in images_toml.read_text(encoding="utf-8").splitlines():
        if line.startswith("tag = "):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def _ensure_k3s_runtime_image(root: Path, timeout: int) -> None:
    tag = _pinned_k3s_runtime_tag(root)
    if not tag:
        return
    image_name = f"cluster/k3s-runtime:{tag}"
    inspect_res = _dispatch("subprocess", subprocess).run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
        check=False,
    )
    if inspect_res.returncode != 0:
        print(f"Building and loading {image_name} into Docker...", flush=True)
        output_root = os.environ.get("BAZEL_OUTPUT_ROOT") or str(root / ".tmp/state/bazel")
        _dispatch("run", run)(
            [
                "bazel",
                f"--output_user_root={output_root}",
                "run",
                "//src/third_party/k3s-io/k3s:load",
            ],
            cwd=root,
            timeout=timeout,
        )


def _resolve_image_tag_and_id(image_ref_or_id: str) -> tuple[str, str]:
    if not image_ref_or_id:
        return "unknown", ""
    res = _dispatch("subprocess", subprocess).run(
        ["docker", "image", "inspect", image_ref_or_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return "unknown", ""
    try:
        data = json.loads(res.stdout)[0]
    except Exception:
        return "unknown", ""
    image_id = str(data.get("Id") or "")
    raw_tags = data.get("RepoTags") or []
    repo_tags = [str(item) for item in raw_tags if isinstance(item, str)]
    for tag in repo_tags:
        if tag.startswith("cluster/k3s-runtime:"):
            return str(tag.split(":", 1)[1]), image_id
    if repo_tags and repo_tags[0] != "<none>:<none>":
        return str(repo_tags[0]), image_id
    return "unknown", image_id


def _collect_node_mount_args(mounts: list[dict[str, Any]]) -> list[str]:
    args: list[str] = []
    for mount in mounts:
        mount_type = mount.get("Type")
        source = mount.get("Name") if mount_type == "volume" else mount.get("Source")
        dest = mount.get("Destination")
        mode = mount.get("Mode")
        if source and dest:
            vol_spec = f"{source}:{dest}" + (f":{mode}" if mode else "")
            args.extend(["-v", vol_spec])
    return args


def _collect_node_network_args(network_settings: dict[str, Any]) -> list[str]:
    networks = network_settings.get("Networks") or {}
    for net_name, net_conf in networks.items():
        if net_name == "bridge":
            continue
        ipam = net_conf.get("IPAMConfig") or {}
        ip_val = ipam.get("IPv4Address") or net_conf.get("IPAddress")
        args: list[str] = ["--network", str(net_name)]
        if ip_val:
            args.extend(["--ip", str(ip_val)])
        return args
    return []


def _collect_node_host_args(host_config: dict[str, Any]) -> list[str]:
    args: list[str] = []
    if host_config.get("Privileged"):
        args.append("--privileged")
    restart_policy = (host_config.get("RestartPolicy") or {}).get("Name", "unless-stopped")
    if restart_policy:
        args.extend(["--restart", str(restart_policy)])
    for extra_host in host_config.get("ExtraHosts") or []:
        args.extend(["--add-host", str(extra_host)])
    for security_opt in host_config.get("SecurityOpt") or []:
        args.extend(["--security-opt", str(security_opt)])
    for container_port, bindings in (host_config.get("PortBindings") or {}).items():
        for binding in bindings:
            host_port = binding.get("HostPort")
            host_ip = binding.get("HostIp", "")
            if host_port:
                prefix = f"{host_ip}:" if host_ip else ""
                args.extend(["-p", f"{prefix}{host_port}:{container_port}"])
    return args


def _collect_node_create_args(
    child: dict[str, Any], pinned_image_name: str
) -> tuple[str, list[str]]:
    name = str(child.get("Name", "")).lstrip("/")
    config = child.get("Config", {})
    entrypoint = config.get("Entrypoint") or []
    cmd = config.get("Cmd") or []
    args: list[str] = ["docker", "create", f"--name={name}"]
    if entrypoint:
        args.append(f"--entrypoint={entrypoint[0]}")
    for var in config.get("Env") or []:
        args.extend(["-e", str(var)])
    for key, value in (config.get("Labels") or {}).items():
        args.extend(["-l", f"{key}={value}"])
    args.extend(_collect_node_host_args(child.get("HostConfig", {})))
    args.extend(_collect_node_mount_args(child.get("Mounts") or []))
    args.extend(_collect_node_network_args(child.get("NetworkSettings", {})))
    args.append(pinned_image_name)
    for c in cmd:
        args.append(str(c))
    return name, args


def _recreate_node_container(
    child: dict[str, Any], pinned_image_name: str, timeout: int
) -> dict[str, Any]:
    """Recreate a retained EKS node container with an upgraded image while preserving volumes."""
    name, args = _collect_node_create_args(child, pinned_image_name)
    print(f"Rolling upgrade for container {name} to {pinned_image_name}...", flush=True)
    _dispatch("run", run)(["docker", "rm", "-f", child["Id"]], timeout=timeout)
    _dispatch("run", run)(args, timeout=timeout)
    return json.loads(_dispatch("run", run)(["docker", "inspect", name]).stdout)[0]


def _validate_retained_node_images(
    fleet: Fleet, children: list[dict[str, Any]], root: Path, timeout: int = 600
) -> None:
    pinned_tag = _pinned_k3s_runtime_tag(root)
    if not pinned_tag:
        return
    pinned_image_name = f"cluster/k3s-runtime:{pinned_tag}"
    _, pinned_id = _dispatch("_resolve_image_tag_and_id", _resolve_image_tag_and_id)(
        pinned_image_name
    )

    node_prefixes = (
        f"/floci-{fleet.namespace}-eks-",
        f"/floci-aws-{fleet.namespace}-eks-",
    )
    for idx, child in enumerate(children):
        child_name = child.get("Name", "")
        if not any(child_name.startswith(prefix) for prefix in node_prefixes):
            continue
        container_image_id = child.get("Image", "")
        current_tag, _ = _dispatch("_resolve_image_tag_and_id", _resolve_image_tag_and_id)(
            container_image_id or child.get("Config", {}).get("Image", "")
        )
        clean_name = child_name.lstrip("/")
        expected_tag_display = pinned_tag if pinned_id else f"{pinned_tag} (unknown)"
        if not pinned_id or container_image_id != pinned_id:
            if not pinned_id:
                raise RuntimeError(
                    f"Container {clean_name} is running image {current_tag}, "
                    f"expected {expected_tag_display}"
                )
            # Perform transparent rolling upgrade of the node container
            new_child = _dispatch("_recreate_node_container", _recreate_node_container)(
                child, pinned_image_name, timeout
            )
            children[idx] = new_child


def _start_services(fleet: Fleet, timeout: int) -> None:
    services_to_up = ["floci", "git"]
    if fleet.git_http_container:
        services_to_up.append("git-http")
    if fleet.headscale_container:
        services_to_up.append("headscale")
    if fleet.git_watcher_container:
        services_to_up.append("git-watcher")
    _dispatch("compose", compose)(
        fleet,
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        str(timeout),
        *services_to_up,
        timeout=timeout + 60,
    )
    if fleet.headscale_container:
        _runtime_symbol("ensure_headscale_user")(fleet)


def _resume_retained_children(fleet: Fleet, children: list[dict[str, Any]], timeout: int) -> None:
    eks_children = [
        child
        for child in children
        if child["Name"].startswith((
            f"/floci-{fleet.namespace}-eks-",
            f"/floci-aws-{fleet.namespace}-eks-",
        ))
    ]
    for child in children:
        current = json.loads(_dispatch("run", run)(["docker", "inspect", child["Name"]]).stdout)[0]
        if current["Id"] != child["Id"]:
            raise RuntimeError(f"Floci replaced retained container {child['Name']}")
        if (current["HostConfig"].get("RestartPolicy") or {}).get("Name") != "unless-stopped":
            raise RuntimeError(f"Floci child {child['Name']} lacks its restart policy")
        if child in eks_children:
            _runtime_symbol("wait_for_cluster")(child["Id"], timeout)
    if eks_children:
        _runtime_symbol("wait_for_floci_clusters")(min(timeout, 60))
        _runtime_symbol("reconcile_eks_containers")(eks_children)


def start(root: Path, timeout: int = 600) -> None:
    """Reconcile the local runtime and resume its retained clusters."""
    print("Starting the local runtime and resuming retained services...", flush=True)
    fleet = _dispatch("configuration", configuration)(root)
    inspected = inspect_containers()
    containers = owned_containers(fleet, inspected)
    parent = next((item for item in containers if item["Name"] == f"/{fleet.container}"), None)
    children = [item for item in containers if item is not parent]
    git = find_git_daemon(fleet, inspected)
    if parent is not None:
        validate_parent(parent, fleet)
    legacy_git = validate_git_daemon(git, fleet) if git is not None else False
    validate_git_address(fleet, inspected, git)
    validate_git_port(inspected, git)
    _runtime_symbol("ensure_storage")(fleet, children)

    _dispatch("_prepare_runtime_images", _prepare_runtime_images)(fleet, root, timeout)
    _validate_retained_node_images(fleet, children, root, timeout=timeout)

    refreshed = inspect_containers()
    current_git = find_git_daemon(fleet, refreshed)
    if (git is None) != (current_git is None) or (
        git is not None and current_git is not None and git["Id"] != current_git["Id"]
    ):
        raise RuntimeError("Local Git daemon changed while its candidate image was prepared")
    if current_git is not None:
        legacy_git = validate_git_daemon(current_git, fleet)
    validate_git_address(fleet, refreshed, current_git)
    validate_git_port(refreshed, current_git)

    if (
        parent is not None
        and (parent["Config"].get("Labels") or {}).get("com.docker.compose.project")
        != fleet.project
    ):
        _dispatch("stop_containers", stop_containers)(containers, fleet, timeout)
        _dispatch("run", run)(["docker", "rm", parent["Id"]])
    if legacy_git and current_git is not None:
        retire_legacy_git_daemon(current_git, timeout)

    _dispatch("_start_services", _start_services)(fleet, timeout)
    if fleet.namespace != "fixture" and any(
        "bridge" in (child.get("NetworkSettings") or {}).get("Networks", {}) for child in children
    ):
        _runtime_symbol("reconcile_floci_bridge")(fleet)
    managed_git = find_git_daemon(fleet, inspect_containers())
    if managed_git is None:
        raise RuntimeError("Local Git daemon is missing after Compose reconciliation")
    validate_managed_git_daemon(managed_git, fleet)
    reconcile_restart_policy(children)
    reconcile_registry(root, required=False)
    if children:
        _dispatch("run", run)(
            ["docker", "start", *(item["Id"] for item in children)], timeout=timeout
        )
        reconcile_registry(root, required=False)
    _dispatch("_resume_retained_children", _resume_retained_children)(fleet, children, timeout)


def stop(root: Path, timeout: int = 300) -> None:
    """Stop the local runtime and its services without deleting persistent data."""
    print("Stopping the local fleet; containers and volumes will be retained...", flush=True)
    fleet = _dispatch("configuration", configuration)(root)
    inspected = inspect_containers()
    containers = owned_containers(fleet, inspected)
    parent = next((item for item in containers if item["Name"] == f"/{fleet.container}"), None)
    git = find_git_daemon(fleet, inspected)
    headscale = find_headscale_daemon(fleet, inspected)
    if parent is not None:
        validate_parent(parent, fleet)
    if git is not None:
        validate_git_daemon(git, fleet)
    _dispatch("stop_containers", stop_containers)(containers, fleet, timeout)
    grace_period = min(timeout, 10)
    for service, name in (
        ("git-watcher", fleet.git_watcher_container),
        ("git-http", fleet.git_http_container),
    ):
        for item in inspected:
            labels = (item.get("Config") or {}).get("Labels") or {}
            if (
                name
                and item["Name"] == f"/{name}"
                and labels.get("com.docker.compose.project") == fleet.project
                and labels.get("com.docker.compose.service") == service
                and item["State"]["Running"]
            ):
                _dispatch("run", run)(
                    ["docker", "stop", "--time", str(grace_period), item["Id"]],
                    timeout=timeout + 30,
                )
    if git is not None and git["State"]["Running"]:
        _dispatch("run", run)(
            ["docker", "stop", "--time", str(grace_period), git["Id"]], timeout=timeout + 30
        )
    if headscale is not None and headscale["State"]["Running"]:
        _dispatch("run", run)(
            ["docker", "stop", "--time", str(grace_period), headscale["Id"]], timeout=timeout + 30
        )


def reconcile_restart_policy(children: list[dict[str, Any]]) -> None:
    """Make retained services recover automatically after an unexpected exit."""
    update = [
        item["Id"]
        for item in children
        if ((item.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name")
        != "unless-stopped"
    ]
    if update:
        _dispatch("run", run)(["docker", "update", "--restart", "unless-stopped", *update])


def stop_containers(containers: list[dict[str, Any]], fleet: Fleet, timeout: int) -> None:
    # Stop Floci first so its recovery loops cannot restart a service while it is stopping.
    parent = [
        item["Id"]
        for item in containers
        if item["Name"] == f"/{fleet.container}" and item["State"]["Running"]
    ]
    children = [
        item["Id"]
        for item in containers
        if item["Name"] != f"/{fleet.container}" and item["State"]["Running"]
    ]
    grace_period = min(timeout, 10)
    for batch in (parent, children):
        if batch:
            _dispatch("run", run)(
                ["docker", "stop", "--time", str(grace_period), *batch], timeout=timeout + 30
            )
