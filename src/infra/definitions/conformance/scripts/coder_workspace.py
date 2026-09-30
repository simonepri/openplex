#!/usr/bin/env python3
"""Drives local Coder workspace lifecycles through OIDC authentication and public APIs to defend workspace provisioning, agent connectivity, and teardown."""

from __future__ import annotations

import argparse
import functools
import http.client
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict, Unpack, cast

from bootstrap_template_publisher import (
    BootstrapError,
    CoderAPI,
    OidcCredentials,
    browser_login,
    logout,
    origin,
    require_https,
)

if TYPE_CHECKING:
    import ssl
    from collections.abc import Iterator, Sequence

DEFAULT_TEMPLATE = "dev"
DEFAULT_ORGANIZATION = "default"
DEFAULT_TIMEOUT_SECONDS = 7_200
POLL_SECONDS = 2.0
TAILNET_CONNECT_ATTEMPTS = 3
TAILNET_CONNECT_RETRY_SECONDS = 1.0
TERMINAL_FAILURES = {"canceled", "failed"}
STARTUP_FAILURES = {"start_error", "start_timeout"}
STARTUP_PENDING = {"created", "starting"}
WORKSPACE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
DEFAULT_SOCKET_TIMEOUT = object()
WORKSPACE_EXEC_SCRIPT = """\
: "${WORKSPACE_CHECKOUT_PATH:?workspace checkout path was not injected}"
: "${WORKSPACE_USERNAME:?workspace user was not injected}"
identity_root=/tmp/workspace-identity
nss_wrapper="$(find /usr/lib -name libnss_wrapper.so -print -quit)"
test -n "$nss_wrapper"
test -r "$identity_root/passwd"
test -r "$identity_root/group"
export LD_PRELOAD="$nss_wrapper"
export NSS_WRAPPER_PASSWD="$identity_root/passwd"
export NSS_WRAPPER_GROUP="$identity_root/group"
export USER="$WORKSPACE_USERNAME"
export LOGNAME="$WORKSPACE_USERNAME"
cd "$WORKSPACE_CHECKOUT_PATH"
exec "$@"
"""


class LifecycleError(Exception):
    """Report a credential-free local workspace lifecycle failure."""


def loopback_socks_address(value: str) -> tuple[str, int]:
    """Parse the isolated tailnet listener without permitting a network bind."""

    match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", value)
    if match is None or int(match.group(1)) > 65_535:
        raise LifecycleError("tailnet SOCKS address must be 127.0.0.1:<port>")
    return "127.0.0.1", int(match.group(1))


def private_gateway_address(value: str) -> str:
    """Parse the RFC 1918 gateway reached through the isolated tailnet."""

    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise LifecycleError("Coder tailnet gateway must be a private IPv4 address") from error
    if not any(
        address in network
        for network in (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
    ):
        raise LifecycleError("Coder tailnet gateway must be a private IPv4 address")
    return str(address)


def receive_exact(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise OSError("tailnet SOCKS proxy closed its response")
        payload.extend(chunk)
    return bytes(payload)


def socks_target(host: str) -> bytes:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        encoded = host.encode("idna")
        if not encoded or len(encoded) > 255:
            raise OSError("tailnet target hostname is invalid") from None
        return b"\x03" + bytes([len(encoded)]) + encoded
    if address.version == 4:
        return b"\x01" + address.packed
    return b"\x04" + address.packed


def connect_through_socks(
    proxy_address: tuple[str, int],
    target_address: tuple[str, int],
    timeout: object = DEFAULT_SOCKET_TIMEOUT,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    if timeout is None or isinstance(timeout, (int, float)):
        connection = socket.create_connection(proxy_address, timeout, source_address)
    else:
        connection = socket.create_connection(proxy_address, source_address=source_address)
    try:
        connection.sendall(b"\x05\x01\x00")
        if receive_exact(connection, 2) != b"\x05\x00":
            raise OSError("tailnet SOCKS proxy rejected unauthenticated access")
        host, port = target_address
        connection.sendall(b"\x05\x01\x00" + socks_target(host) + struct.pack("!H", port))
        version, status, reserved, address_type = receive_exact(connection, 4)
        if version != 5 or status != 0 or reserved != 0:
            raise OSError("tailnet SOCKS proxy rejected the private endpoint")
        if address_type == 1:
            address_size = 4
        elif address_type == 3:
            address_size = receive_exact(connection, 1)[0]
        elif address_type == 4:
            address_size = 16
        else:
            raise OSError("tailnet SOCKS proxy returned an invalid address")
        receive_exact(connection, address_size + 2)
        return connection
    except BaseException:
        connection.close()
        raise


class HTTPSConnectionOptions(TypedDict, total=False):
    """Describe the standard HTTPS connection options forwarded by urllib."""

    port: int | None
    timeout: float | None
    source_address: tuple[str, int] | None
    context: ssl.SSLContext | None
    blocksize: int


class TailnetHTTPSConnection(http.client.HTTPSConnection):
    """Open an HTTPS connection through one isolated tailnet SOCKS listener."""

    def __init__(
        self,
        host: str,
        *,
        socks_address: tuple[str, int],
        target_addresses: dict[str, str],
        **kwargs: Unpack[HTTPSConnectionOptions],
    ) -> None:
        super().__init__(host, **kwargs)
        self._socks_address = socks_address
        self._target_addresses = target_addresses
        self._create_connection = self._connect_through_tailnet

    def _connect_through_tailnet(
        self,
        address: tuple[str, int],
        timeout: object = DEFAULT_SOCKET_TIMEOUT,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        host, port = address
        target_address = self._target_addresses.get(host.lower())
        if target_address is None:
            raise OSError("tailnet target hostname is not mapped")
        for attempt in range(TAILNET_CONNECT_ATTEMPTS):
            try:
                return connect_through_socks(
                    self._socks_address,
                    (target_address, port),
                    timeout,
                    source_address,
                )
            except OSError:
                if attempt == TAILNET_CONNECT_ATTEMPTS - 1:
                    raise
                time.sleep(TAILNET_CONNECT_RETRY_SECONDS)
        raise AssertionError("tailnet connection retry loop did not return or raise")


class TailnetHTTPSHandler(urllib.request.HTTPSHandler):
    """Route mapped HTTPS endpoints through the isolated tailnet."""

    _context: ssl.SSLContext

    def __init__(
        self,
        socks_address: tuple[str, int],
        target_addresses: dict[str, str],
    ) -> None:
        super().__init__()
        self.socks_address = socks_address
        self.target_addresses = target_addresses

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        connection = functools.partial(
            TailnetHTTPSConnection,
            socks_address=self.socks_address,
            target_addresses=self.target_addresses,
        )
        return self.do_open(connection, req, context=self._context)


def tailnet_handlers(
    socks_address: tuple[str, int],
    target_addresses: dict[str, str],
) -> tuple[urllib.request.BaseHandler, ...]:
    return (
        urllib.request.ProxyHandler({}),
        TailnetHTTPSHandler(socks_address, target_addresses),
    )


class API(Protocol):
    """Describe the subset of CoderAPI used by the lifecycle."""

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        accepted: set[int],
    ) -> tuple[int, object | None]: ...


@dataclass(frozen=True)
class WorkspaceResources:
    """Identify the cell resources owned by one running workspace."""

    namespace: str
    deployment: str
    pvc: str
    pvc_uid: str


class KubernetesAccess(Protocol):
    """Describe resource discovery and execution used by the lifecycle."""

    def running_resources(self, workspace_id: str, deadline: float) -> WorkspaceResources: ...

    def wait_stopped(self, workspace_id: str, deadline: float) -> None: ...

    def wait_deleted(self, workspace_id: str, deadline: float) -> None: ...

    def exec(
        self,
        resources: WorkspaceResources,
        command: Sequence[str],
        deadline: float,
    ) -> int: ...


def mapping(value: object | None, subject: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LifecycleError(f"Coder returned an invalid {subject}")
    return cast("dict[str, object]", value)


def string_field(value: dict[str, object], field: str, subject: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise LifecycleError(f"Coder returned an invalid {subject} {field}")
    return result


def uuid_field(value: dict[str, object], field: str, subject: str) -> str:
    result = string_field(value, field, subject)
    try:
        uuid.UUID(result)
    except ValueError as error:
        raise LifecycleError(f"Coder returned an invalid {subject} {field}") from error
    return result


def object_list_field(
    value: dict[str, object], field: str, subject: str
) -> list[dict[str, object]]:
    result = value.get(field)
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise LifecycleError(f"Coder returned an invalid {subject} {field}")
    return cast("list[dict[str, object]]", result)


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LifecycleError("local workspace lifecycle exceeded its deadline")
    return remaining


class KubectlWorkspaceAccess:
    """Discover and execute against exact workspace-labelled Kubernetes resources."""

    def __init__(self, executable: str = "kubectl") -> None:
        self.executable = executable

    def _json(self, arguments: Sequence[str], deadline: float) -> dict[str, object]:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, remaining_seconds(deadline)),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LifecycleError("kubectl could not inspect the local workspace") from error
        if completed.returncode != 0:
            raise LifecycleError("kubectl could not inspect the local workspace")
        try:
            return mapping(json.loads(completed.stdout), "Kubernetes resource list")
        except json.JSONDecodeError as error:
            raise LifecycleError("kubectl returned invalid workspace resource JSON") from error

    @staticmethod
    def _items(value: dict[str, object]) -> list[dict[str, object]]:
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise LifecycleError("kubectl returned an invalid workspace resource list")
        if not all(isinstance(item, dict) for item in raw_items):
            raise LifecycleError("kubectl returned an invalid workspace resource")
        return cast("list[dict[str, object]]", raw_items)

    @staticmethod
    def _metadata(value: dict[str, object], subject: str) -> dict[str, object]:
        return mapping(value.get("metadata"), f"Kubernetes {subject} metadata")

    def _deployments(self, workspace_id: str, deadline: float) -> list[dict[str, object]]:
        return self._items(
            self._json(
                [
                    "get",
                    "deployments",
                    "--all-namespaces",
                    "--selector",
                    f"com.coder.workspace.id={workspace_id}",
                    "--output=json",
                ],
                deadline,
            )
        )

    def _pvcs(self, workspace_id: str, namespace: str, deadline: float) -> list[dict[str, object]]:
        return self._items(
            self._json(
                [
                    "get",
                    "persistentvolumeclaims",
                    f"--namespace={namespace}",
                    "--selector",
                    f"com.coder.workspace.id={workspace_id}",
                    "--output=json",
                ],
                deadline,
            )
        )

    def _all_pvcs(self, workspace_id: str, deadline: float) -> list[dict[str, object]]:
        return self._items(
            self._json(
                [
                    "get",
                    "persistentvolumeclaims",
                    "--all-namespaces",
                    "--selector",
                    f"com.coder.workspace.id={workspace_id}",
                    "--output=json",
                ],
                deadline,
            )
        )

    def running_resources(self, workspace_id: str, deadline: float) -> WorkspaceResources:
        while True:
            deployments = self._deployments(workspace_id, deadline)
            if len(deployments) > 1:
                raise LifecycleError("workspace owns more than one Deployment")
            if deployments:
                deployment = deployments[0]
                metadata = self._metadata(deployment, "Deployment")
                namespace = string_field(metadata, "namespace", "Kubernetes Deployment")
                deployment_name = string_field(metadata, "name", "Kubernetes Deployment")
                status = mapping(deployment.get("status", {}), "Kubernetes Deployment status")
                available = status.get("availableReplicas", 0)
                pvcs = self._pvcs(workspace_id, namespace, deadline)
                if len(pvcs) > 1:
                    raise LifecycleError("workspace owns more than one persistent volume claim")
                if pvcs and isinstance(available, int) and available == 1:
                    pvc = pvcs[0]
                    pvc_metadata = self._metadata(pvc, "persistent volume claim")
                    pvc_status = mapping(
                        pvc.get("status", {}), "Kubernetes persistent volume claim status"
                    )
                    if pvc_status.get("phase") == "Bound":
                        return WorkspaceResources(
                            namespace=namespace,
                            deployment=deployment_name,
                            pvc=string_field(
                                pvc_metadata, "name", "Kubernetes persistent volume claim"
                            ),
                            pvc_uid=uuid_field(
                                pvc_metadata, "uid", "Kubernetes persistent volume claim"
                            ),
                        )
            remaining_seconds(deadline)
            time.sleep(POLL_SECONDS)

    def wait_stopped(self, workspace_id: str, deadline: float) -> None:
        while True:
            if self._deployments(workspace_id, deadline):
                remaining_seconds(deadline)
                time.sleep(POLL_SECONDS)
                continue
            pvcs = self._all_pvcs(workspace_id, deadline)
            if len(pvcs) != 1:
                raise LifecycleError("stopped workspace did not retain exactly one PVC")
            status = mapping(pvcs[0].get("status", {}), "stopped workspace PVC status")
            if status.get("phase") == "Bound":
                return
            remaining_seconds(deadline)
            time.sleep(POLL_SECONDS)

    def wait_deleted(self, workspace_id: str, deadline: float) -> None:
        while True:
            deployments = self._deployments(workspace_id, deadline)
            pvcs = self._all_pvcs(workspace_id, deadline)
            if not deployments and not pvcs:
                return
            remaining_seconds(deadline)
            time.sleep(POLL_SECONDS)

    def exec(
        self,
        resources: WorkspaceResources,
        command: Sequence[str],
        deadline: float,
    ) -> int:
        try:
            completed = subprocess.run(
                [
                    self.executable,
                    "exec",
                    f"--namespace={resources.namespace}",
                    f"deployment/{resources.deployment}",
                    "--",
                    "/bin/sh",
                    "-ceu",
                    WORKSPACE_EXEC_SCRIPT,
                    "local-coder-workspace",
                    *command,
                ],
                check=False,
                timeout=max(1.0, remaining_seconds(deadline)),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LifecycleError("kubectl could not execute in the local workspace") from error
        return completed.returncode


class WorkspaceLifecycle:
    """Drive one authenticated Coder API session and its Kubernetes resources."""

    def __init__(
        self,
        api: API,
        kubernetes: KubernetesAccess,
        owner_id: str,
        *,
        organization: str = DEFAULT_ORGANIZATION,
        template: str = DEFAULT_TEMPLATE,
    ) -> None:
        self.api = api
        self.kubernetes = kubernetes
        self.owner_id = owner_id
        self.organization = organization
        self.template = template

    def workspace(self, name: str, *, allow_missing: bool = False) -> dict[str, object] | None:
        encoded_name = urllib.parse.quote(name, safe="")
        status, value = self.api.request(
            "GET",
            f"/api/v2/users/me/workspace/{encoded_name}",
            accepted={200, 404} if allow_missing else {200},
        )
        if status == 404:
            return None
        return mapping(value, "workspace")

    def _template_id(self) -> str:
        organization = urllib.parse.quote(self.organization, safe="")
        template = urllib.parse.quote(self.template, safe="")
        _, value = self.api.request(
            "GET",
            f"/api/v2/organizations/{organization}/templates/{template}",
            accepted={200},
        )
        template_value = mapping(value, "template")
        if string_field(template_value, "name", "template") != self.template:
            raise LifecycleError("Coder returned the wrong workspace template")
        return uuid_field(template_value, "id", "template")

    def _build(self, build_id: str) -> dict[str, object]:
        build_id = str(uuid.UUID(build_id))
        _, value = self.api.request(
            "GET",
            f"/api/v2/workspacebuilds/{build_id}",
            accepted={200},
        )
        return mapping(value, "workspace build")

    def _wait_build(self, build_id: str, expected_status: str, deadline: float) -> None:
        while True:
            build = self._build(build_id)
            status = string_field(build, "status", "workspace build")
            if status == expected_status:
                return
            if status in TERMINAL_FAILURES:
                raise LifecycleError(f"Coder workspace build finished as {status}")
            remaining_seconds(deadline)
            time.sleep(POLL_SECONDS)

    def _wait_startup_ready(self, build_id: str, deadline: float) -> None:
        while True:
            build = self._build(build_id)
            if string_field(build, "status", "workspace build") != "running":
                raise LifecycleError("Coder workspace build left running during startup")
            agents: list[dict[str, object]] = []
            for resource in object_list_field(build, "resources", "workspace build"):
                raw_agents = resource.get("agents", [])
                if not isinstance(raw_agents, list) or not all(
                    isinstance(agent, dict) for agent in raw_agents
                ):
                    raise LifecycleError("Coder returned invalid workspace resource agents")
                agents.extend(raw_agents)
            if len(agents) > 1:
                raise LifecycleError("workspace build owns more than one Coder agent")
            if agents:
                agent = agents[0]
                state = string_field(agent, "lifecycle_state", "workspace agent")
                status = string_field(agent, "status", "workspace agent")
                if state == "ready" and status == "connected":
                    return
                if state in STARTUP_FAILURES:
                    raise LifecycleError(f"Coder workspace startup finished as {state}")
                if state not in STARTUP_PENDING and state != "ready":
                    raise LifecycleError(f"Coder workspace startup entered invalid state {state}")
                if status not in {"connected", "connecting", "disconnected", "timeout"}:
                    raise LifecycleError(f"Coder workspace agent entered invalid status {status}")
            remaining_seconds(deadline)
            time.sleep(POLL_SECONDS)

    def _wait_initial_build(self, workspace: dict[str, object], deadline: float) -> None:
        latest_build = mapping(workspace.get("latest_build"), "initial workspace build")
        build_id = uuid_field(latest_build, "id", "initial workspace build")
        self._wait_build(build_id, "running", deadline)
        self._wait_startup_ready(build_id, deadline)

    def create(
        self,
        name: str,
        restore_selector: str | None,
        deadline: float,
    ) -> dict[str, str]:
        validate_name(name)
        if self.workspace(name, allow_missing=True) is not None:
            raise LifecycleError("workspace name already exists")
        parameters = []
        if restore_selector is not None:
            if not restore_selector or len(restore_selector) > 128:
                raise LifecycleError("restore selector is invalid")
            parameters.append({"name": "restore_selector", "value": restore_selector})
        _, value = self.api.request(
            "POST",
            "/api/v2/users/me/workspaces",
            payload={
                "name": name,
                "rich_parameter_values": parameters,
                "template_id": self._template_id(),
            },
            accepted={201},
        )
        workspace = mapping(value, "created workspace")
        workspace_id = uuid_field(workspace, "id", "created workspace")
        owner_id = uuid_field(workspace, "owner_id", "created workspace")
        if (
            owner_id != self.owner_id
            or string_field(workspace, "name", "created workspace") != name
        ):
            raise LifecycleError("Coder created a workspace outside the authenticated owner")
        self._wait_initial_build(workspace, deadline)
        resources = self.kubernetes.running_resources(workspace_id, deadline)
        return {
            "deployment": resources.deployment,
            "id": workspace_id,
            "name": name,
            "namespace": resources.namespace,
            "ownerId": owner_id,
            "pvc": resources.pvc,
            "pvcUid": resources.pvc_uid,
        }

    def transition(self, name: str, transition: str, deadline: float) -> dict[str, str]:
        workspace = self.workspace(name)
        assert workspace is not None
        workspace_id = uuid_field(workspace, "id", "workspace")
        _, value = self.api.request(
            "POST",
            f"/api/v2/workspaces/{workspace_id}/builds",
            payload={"reason": "cli", "transition": transition},
            accepted={201},
        )
        build = mapping(value, "workspace build")
        expected = {"delete": "deleted", "start": "running", "stop": "stopped"}[transition]
        build_id = uuid_field(build, "id", "workspace build")
        self._wait_build(build_id, expected, deadline)
        if transition == "start":
            self.kubernetes.running_resources(workspace_id, deadline)
            self._wait_startup_ready(build_id, deadline)
        elif transition == "stop":
            self.kubernetes.wait_stopped(workspace_id, deadline)
        else:
            self.kubernetes.wait_deleted(workspace_id, deadline)
        return {"id": workspace_id, "name": name, "status": expected}

    def exec(self, name: str, command: Sequence[str], deadline: float) -> int:
        if not command:
            raise LifecycleError("workspace exec requires a command")
        workspace = self.workspace(name)
        assert workspace is not None
        workspace_id = uuid_field(workspace, "id", "workspace")
        latest_build = mapping(workspace.get("latest_build"), "workspace build")
        if string_field(latest_build, "status", "workspace build") != "running":
            raise LifecycleError("workspace must be running before exec")
        resources = self.kubernetes.running_resources(workspace_id, deadline)
        self._wait_startup_ready(uuid_field(latest_build, "id", "workspace build"), deadline)
        return self.kubernetes.exec(resources, command, deadline)


def validate_name(name: str) -> None:
    if not WORKSPACE_NAME.fullmatch(name):
        raise LifecycleError("workspace name must be a canonical 1-32 character label")


@contextmanager
def authenticated_lifecycle() -> Iterator[WorkspaceLifecycle]:
    coder_url = os.environ["CODER_URL"]
    bootstrap_url = os.environ.get("CODER_BOOTSTRAP_URL", coder_url)
    issuer_url = os.environ["CODER_BOOTSTRAP_OIDC_ISSUER"]
    username = os.environ["CODER_BOOTSTRAP_OIDC_USERNAME"]
    password = os.environ["CODER_BOOTSTRAP_OIDC_PASSWORD"]
    socks_address = loopback_socks_address(os.environ["CODER_TAILNET_SOCKS_ADDRESS"])
    gateway_address = private_gateway_address(os.environ["CODER_TAILNET_GATEWAY_ADDRESS"])
    require_https(coder_url, allow_insecure=False)
    require_https(bootstrap_url, allow_insecure=False)
    require_https(issuer_url, allow_insecure=False)
    origin(coder_url)
    endpoint_hosts = {
        host
        for url in (coder_url, bootstrap_url, issuer_url)
        if (host := urllib.parse.urlsplit(url).hostname) is not None
    }
    target_addresses = {host.lower(): gateway_address for host in endpoint_hosts}

    session_token = browser_login(
        bootstrap_url,
        issuer_url,
        OidcCredentials(username=username, password=password),
        handlers=tailnet_handlers(socks_address, target_addresses),
    )
    try:
        api = CoderAPI(
            coder_url,
            session_token,
            handlers=tailnet_handlers(socks_address, target_addresses),
        )
        _, value = api.request("GET", "/api/v2/users/me", accepted={200})
        user = mapping(value, "current user")
        roles = user.get("roles")
        if not isinstance(roles, list):
            raise LifecycleError("Coder returned invalid current-user roles")
        role_names = {role.get("name") for role in roles if isinstance(role, dict)}
        owner_id = uuid_field(user, "id", "current user")
        if (
            user.get("email") != username
            or user.get("login_type") != "oidc"
            or "owner" not in role_names
        ):
            raise LifecycleError("local OIDC fixture is not the Coder owner")
        yield WorkspaceLifecycle(
            api,
            KubectlWorkspaceAccess(os.environ.get("KUBECTL", "kubectl")),
            owner_id,
            organization=os.environ.get("CODER_ORGANIZATION", DEFAULT_ORGANIZATION),
        )
    finally:
        try:
            logout(
                coder_url,
                session_token,
                handlers=tailnet_handlers(socks_address, target_addresses),
            )
        except BootstrapError:
            if sys.exception() is None:
                raise LifecycleError("Coder rejected local workspace logout") from None


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.environ.get("CODER_WORKSPACE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    )
    commands = value.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("--restore-selector")
    for action in ("delete", "start", "stop"):
        command = commands.add_parser(action)
        command.add_argument("name")
    execute = commands.add_parser("exec")
    execute.add_argument("name")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    return value


def run(arguments: Sequence[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    if not 30 <= options.timeout_seconds <= 7_200:
        raise LifecycleError("workspace timeout must be between 30 and 7200 seconds")
    deadline = time.monotonic() + options.timeout_seconds
    with authenticated_lifecycle() as lifecycle:
        if options.action == "create":
            result = lifecycle.create(
                options.name,
                options.restore_selector,
                deadline,
            )
        elif options.action == "exec":
            command = options.command[1:] if options.command[:1] == ["--"] else options.command
            return lifecycle.exec(options.name, command, deadline)
        else:
            result = lifecycle.transition(options.name, options.action, deadline)
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (BootstrapError, KeyError, LifecycleError, OSError, ValueError) as error:
        message = error.args[0] if error.args else "local workspace lifecycle failed"
        raise SystemExit(f"Local Coder workspace failed: {message}") from None
