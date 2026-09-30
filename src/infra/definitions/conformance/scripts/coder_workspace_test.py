#!/usr/bin/env python3
"""Tests local Coder workspace lifecycle payloads, wait conditions, ownership boundaries, and cleanup routines offline to prevent test harness regressions."""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from typing import TYPE_CHECKING
from unittest import mock

import coder_workspace

if TYPE_CHECKING:
    from collections.abc import Sequence

OWNER_ID = "8826ee2e-7933-4665-aef2-2393f84a0d05"
WORKSPACE_ID = "0967198e-ec7b-4c6b-b4d3-f71244cadbe9"
TEMPLATE_ID = "c6d67e98-83ea-49f0-8812-e4abae2b68bc"
BUILD_ID = "badaf2eb-96c5-4050-9f1d-db2d39ca5478"
PVC_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"


def workspace(name: str, status: str = "running") -> dict[str, object]:
    return {
        "id": WORKSPACE_ID,
        "latest_build": {"id": BUILD_ID, "status": status},
        "name": name,
        "owner_id": OWNER_ID,
    }


class FakeAPI:
    def __init__(self) -> None:
        self.existing: dict[str, object] | None = None
        self.build_status = "running"
        self.agent_lifecycle = "ready"
        self.agent_status = "connected"
        self.requests: list[tuple[str, str, object | None]] = []
        self.user: dict[str, object] | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        accepted: set[int],
    ) -> tuple[int, object | None]:
        self.requests.append((method, path, payload))
        if method == "GET":
            return self._get(path, accepted)
        return self._post(path, payload)

    def _get(self, path: str, accepted: set[int]) -> tuple[int, object | None]:
        if path == "/api/v2/users/me" and self.user is not None:
            return 200, self.user
        if path == "/api/v2/organizations/default/templates/dev":
            return 200, {"id": TEMPLATE_ID, "name": "dev"}
        if path.startswith("/api/v2/users/me/workspace/"):
            if self.existing is None:
                if 404 not in accepted:
                    raise coder_workspace.LifecycleError("missing fake workspace")
                return 404, None
            return 200, self.existing
        if path == f"/api/v2/workspacebuilds/{BUILD_ID}":
            return 200, {
                "resources": [
                    {
                        "agents": [
                            {
                                "lifecycle_state": self.agent_lifecycle,
                                "status": self.agent_status,
                            }
                        ]
                    }
                ],
                "status": self.build_status,
            }
        raise AssertionError(f"unexpected GET request: {path}")

    def _post(self, path: str, payload: object | None) -> tuple[int, object | None]:
        if path == "/api/v2/users/me/workspaces":
            assert isinstance(payload, dict)
            self.existing = workspace(str(payload["name"]), "starting")
            return 201, self.existing
        if path == f"/api/v2/workspaces/{WORKSPACE_ID}/builds":
            assert isinstance(payload, dict)
            self.build_status = {
                "delete": "deleted",
                "start": "running",
                "stop": "stopped",
            }[str(payload["transition"])]
            return 201, {"id": BUILD_ID}
        raise AssertionError(f"unexpected POST request: {path}")


class FakeKubernetes:
    def __init__(self) -> None:
        self.resources = coder_workspace.WorkspaceResources(
            namespace="team-examples-workspaces",
            deployment="coder-workspace-acceptance",
            pvc="coder-workspace-acceptance-home",
            pvc_uid=PVC_ID,
        )
        self.calls: list[tuple[str, object]] = []
        self.exec_status = 0

    def running_resources(
        self, workspace_id: str, deadline: float
    ) -> coder_workspace.WorkspaceResources:
        self.calls.append(("running", workspace_id))
        return self.resources

    def wait_stopped(self, workspace_id: str, deadline: float) -> None:
        self.calls.append(("stopped", workspace_id))

    def wait_deleted(self, workspace_id: str, deadline: float) -> None:
        self.calls.append(("deleted", workspace_id))

    def exec(
        self,
        resources: coder_workspace.WorkspaceResources,
        command: Sequence[str],
        deadline: float,
    ) -> int:
        self.calls.append(("exec", (resources, command)))
        return self.exec_status


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = bytearray(response)
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, size: int) -> bytes:
        payload = bytes(self.response[:size])
        del self.response[:size]
        return payload

    def close(self) -> None:
        self.closed = True


class WorkspaceLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeAPI()
        self.kubernetes = FakeKubernetes()
        self.lifecycle = coder_workspace.WorkspaceLifecycle(
            self.api,
            self.kubernetes,
            OWNER_ID,
        )
        self.deadline = coder_workspace.time.monotonic() + 60

    def test_create_uses_active_dev_template_and_exact_rich_parameters(self) -> None:
        result = self.lifecycle.create("backup-source", "snapshot-1", self.deadline)

        self.assertEqual(
            result,
            {
                "deployment": "coder-workspace-acceptance",
                "id": WORKSPACE_ID,
                "name": "backup-source",
                "namespace": "team-examples-workspaces",
                "ownerId": OWNER_ID,
                "pvc": "coder-workspace-acceptance-home",
                "pvcUid": PVC_ID,
            },
        )
        self.assertIn(
            (
                "POST",
                "/api/v2/users/me/workspaces",
                {
                    "name": "backup-source",
                    "rich_parameter_values": [{"name": "restore_selector", "value": "snapshot-1"}],
                    "template_id": TEMPLATE_ID,
                },
            ),
            self.api.requests,
        )
        self.assertIn(("running", WORKSPACE_ID), self.kubernetes.calls)

    def test_create_rejects_an_existing_workspace_before_mutation(self) -> None:
        self.api.existing = workspace("backup-source")

        with self.assertRaisesRegex(
            coder_workspace.LifecycleError, "workspace name already exists"
        ):
            self.lifecycle.create("backup-source", None, self.deadline)

        self.assertFalse(any(method == "POST" for method, _path, _payload in self.api.requests))

    def test_start_stop_and_delete_wait_for_builds_and_resource_state(self) -> None:
        self.api.existing = workspace("backup-source")

        started = self.lifecycle.transition("backup-source", "start", self.deadline)
        stopped = self.lifecycle.transition("backup-source", "stop", self.deadline)
        deleted = self.lifecycle.transition("backup-source", "delete", self.deadline)

        self.assertEqual(started["status"], "running")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(
            self.kubernetes.calls,
            [
                ("running", WORKSPACE_ID),
                ("stopped", WORKSPACE_ID),
                ("deleted", WORKSPACE_ID),
            ],
        )
        transition_payloads = [
            payload
            for method, path, payload in self.api.requests
            if method == "POST" and path.endswith("/builds")
        ]
        self.assertEqual(
            transition_payloads,
            [
                {"reason": "cli", "transition": "start"},
                {"reason": "cli", "transition": "stop"},
                {"reason": "cli", "transition": "delete"},
            ],
        )

    def test_build_failure_does_not_report_a_successful_transition(self) -> None:
        self.api.existing = workspace("backup-source")
        self.api.build_status = "failed"

        with self.assertRaisesRegex(coder_workspace.LifecycleError, "finished as failed"):
            self.lifecycle._wait_build(BUILD_ID, "running", self.deadline)

        self.assertEqual(self.kubernetes.calls, [])

    def test_create_waits_for_the_coder_agent_startup_lifecycle(self) -> None:
        self.api.agent_lifecycle = "starting"

        def finish_startup(_seconds: float) -> None:
            self.api.agent_lifecycle = "ready"

        with mock.patch.object(coder_workspace.time, "sleep", side_effect=finish_startup):
            self.lifecycle.create("backup-source", None, self.deadline)

        self.assertEqual(self.api.agent_lifecycle, "ready")

    def test_create_rejects_a_failed_startup_lifecycle(self) -> None:
        self.api.agent_lifecycle = "start_error"

        with self.assertRaisesRegex(
            coder_workspace.LifecycleError, "startup finished as start_error"
        ):
            self.lifecycle.create("backup-source", None, self.deadline)

    def test_exec_requires_running_workspace_and_preserves_argument_boundaries(self) -> None:
        self.api.existing = workspace("backup-source")
        command = ["/bin/sh", "-ceu", "printf '%s' 'one two'"]

        status = self.lifecycle.exec("backup-source", command, self.deadline)

        self.assertEqual(status, 0)
        self.assertEqual(
            self.kubernetes.calls,
            [
                ("running", WORKSPACE_ID),
                ("exec", (self.kubernetes.resources, command)),
            ],
        )


class AuthenticatedRunTest(unittest.TestCase):
    def test_socks_connection_resolves_the_private_hostname_inside_the_tailnet(self) -> None:
        connection = FakeSocket(b"\x05\x00\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
        hostname = "coder.ctrl-eaws-lh1.k8s.example.invalid"
        with mock.patch.object(
            coder_workspace.socket,
            "create_connection",
            return_value=connection,
        ) as create_connection:
            result = coder_workspace.connect_through_socks(
                ("127.0.0.1", 1055),
                (hostname, 443),
                3,
            )

        self.assertIs(result, connection)
        create_connection.assert_called_once_with(("127.0.0.1", 1055), 3, None)
        self.assertEqual(connection.sent[0], b"\x05\x01\x00")
        self.assertEqual(
            connection.sent[1],
            b"\x05\x01\x00\x03" + bytes([len(hostname)]) + hostname.encode() + b"\x01\xbb",
        )
        self.assertFalse(connection.closed)

    def test_socks_listener_must_be_loopback(self) -> None:
        for address in ("0.0.0.0:1055", "127.0.0.1:0", "127.0.0.1:65536"):
            with self.subTest(address=address):
                with self.assertRaisesRegex(
                    coder_workspace.LifecycleError,
                    "must be 127.0.0.1:<port>",
                ):
                    coder_workspace.loopback_socks_address(address)

    def test_https_connection_maps_the_hostname_to_the_private_gateway(self) -> None:
        hostname = "coder.ctrl-eaws-lh1.k8s.example.invalid"
        connection = FakeSocket(b"")
        client = coder_workspace.TailnetHTTPSConnection(
            hostname,
            socks_address=("127.0.0.1", 1055),
            target_addresses={hostname: "172.31.0.11"},
        )
        with mock.patch.object(
            coder_workspace,
            "connect_through_socks",
            return_value=connection,
        ) as connect:
            result = client._connect_through_tailnet((hostname, 443), 3)

        self.assertIs(result, connection)
        self.assertEqual(client.host, hostname)
        connect.assert_called_once_with(
            ("127.0.0.1", 1055),
            ("172.31.0.11", 443),
            3,
            None,
        )

    def test_https_connection_rejects_an_unmapped_hostname(self) -> None:
        hostname = "coder.ctrl-eaws-lh1.k8s.example.invalid"
        client = coder_workspace.TailnetHTTPSConnection(
            hostname,
            socks_address=("127.0.0.1", 1055),
            target_addresses={hostname: "172.31.0.11"},
        )

        with self.assertRaisesRegex(OSError, "tailnet target hostname is not mapped"):
            client._connect_through_tailnet(("public.example.invalid", 443), 3)

    def test_https_connection_retries_a_transient_tailnet_failure(self) -> None:
        hostname = "coder.ctrl-eaws-lh1.k8s.example.invalid"
        connection = FakeSocket(b"")
        client = coder_workspace.TailnetHTTPSConnection(
            hostname,
            socks_address=("127.0.0.1", 1055),
            target_addresses={hostname: "172.31.0.11"},
        )
        with (
            mock.patch.object(
                coder_workspace,
                "connect_through_socks",
                side_effect=(OSError("context deadline exceeded"), connection),
            ) as connect,
            mock.patch.object(coder_workspace.time, "sleep") as sleep,
        ):
            result = client._connect_through_tailnet((hostname, 443), 3)

        self.assertIs(result, connection)
        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(coder_workspace.TAILNET_CONNECT_RETRY_SECONDS)

    def test_tailnet_gateway_must_be_rfc_1918(self) -> None:
        self.assertEqual(
            coder_workspace.private_gateway_address("172.31.0.11"),
            "172.31.0.11",
        )
        for address in ("not-an-address", "100.64.0.1", "203.0.113.1"):
            with self.subTest(address=address):
                with self.assertRaisesRegex(
                    coder_workspace.LifecycleError,
                    "must be a private IPv4 address",
                ):
                    coder_workspace.private_gateway_address(address)

    def test_cli_uses_oidc_owner_session_and_always_logs_out(self) -> None:
        api = FakeAPI()
        api.existing = workspace("backup-source")
        user: dict[str, object] = {
            "email": "operator@example.invalid",
            "id": OWNER_ID,
            "login_type": "oidc",
            "roles": [{"name": "owner"}],
        }
        api.user = user
        output = io.StringIO()
        environment = {
            "CODER_BOOTSTRAP_OIDC_ISSUER": "https://dex.ctrl.k8s.example.invalid",
            "CODER_BOOTSTRAP_OIDC_PASSWORD": "fixture-password",
            "CODER_BOOTSTRAP_OIDC_USERNAME": "operator@example.invalid",
            "CODER_TAILNET_GATEWAY_ADDRESS": "172.31.0.11",
            "CODER_TAILNET_SOCKS_ADDRESS": "127.0.0.1:1055",
            "CODER_URL": "https://coder.ctrl.k8s.example.invalid",
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(
                coder_workspace, "browser_login", return_value="session"
            ) as browser_login,
            mock.patch.object(coder_workspace, "CoderAPI", return_value=api) as coder_api,
            mock.patch.object(
                coder_workspace, "KubectlWorkspaceAccess", return_value=FakeKubernetes()
            ),
            mock.patch.object(coder_workspace, "logout") as logout,
            redirect_stdout(output),
        ):
            status = coder_workspace.run(["--timeout-seconds", "30", "stop", "backup-source"])

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "stopped")
        browser_login.assert_called_once()
        self.assertEqual(
            browser_login.call_args.args,
            (
                "https://coder.ctrl.k8s.example.invalid",
                "https://dex.ctrl.k8s.example.invalid",
                coder_workspace.OidcCredentials(
                    username="operator@example.invalid",
                    password="fixture-password",
                ),
            ),
        )
        coder_api.assert_called_once()
        logout.assert_called_once()
        for call in (browser_login.call_args, coder_api.call_args, logout.call_args):
            handlers = call.kwargs["handlers"]
            self.assertEqual(len(handlers), 2)
            self.assertIsInstance(handlers[0], coder_workspace.urllib.request.ProxyHandler)
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], coder_workspace.TailnetHTTPSHandler)
            self.assertEqual(handlers[1].socks_address, ("127.0.0.1", 1055))
            self.assertEqual(
                handlers[1].target_addresses,
                {
                    "coder.ctrl.k8s.example.invalid": "172.31.0.11",
                    "dex.ctrl.k8s.example.invalid": "172.31.0.11",
                },
            )
        self.assertEqual(
            logout.call_args.args,
            ("https://coder.ctrl.k8s.example.invalid", "session"),
        )

    def test_kubectl_exec_restores_checkout_and_workspace_identity(self) -> None:
        resources = coder_workspace.WorkspaceResources(
            "team-examples-workspaces", "workspace", "workspace-home", PVC_ID
        )
        with mock.patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            status = coder_workspace.KubectlWorkspaceAccess("kubectl-fixture").exec(
                resources,
                ["printf", "%s", "one two"],
                coder_workspace.time.monotonic() + 30,
            )

        self.assertEqual(status, 0)
        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments,
            [
                "kubectl-fixture",
                "exec",
                "--namespace=team-examples-workspaces",
                "deployment/workspace",
                "--",
                "/bin/sh",
                "-ceu",
                coder_workspace.WORKSPACE_EXEC_SCRIPT,
                "local-coder-workspace",
                "printf",
                "%s",
                "one two",
            ],
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_default_deadline_contains_ray_train_acceptance(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            options = coder_workspace.parser().parse_args([
                "exec",
                "ray-train-acceptance",
                "true",
            ])

        self.assertEqual(options.timeout_seconds, 7_200)


if __name__ == "__main__":
    unittest.main()
