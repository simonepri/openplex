#!/usr/bin/env python3
"""Tests local kubeconfigs, access mesh routing, browser PAC generation, and SOCKS5 proxying."""

from __future__ import annotations

import base64
import copy
import io
import ipaddress
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from infra.tools.cloud_emulator.access import (
    browser_proxy,
    cluster_browser,
    cluster_tailnet,
    kubeconfig,
    workspace_ssh_proxy,
)


class BaseAccessTestCase(unittest.TestCase):
    """Base test case suppressing noisy workflow stdout output."""

    def setUp(self) -> None:
        super().setUp()
        self._stdout_patcher = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.mock_stdout = self._stdout_patcher.start()

    def tearDown(self) -> None:
        self._stdout_patcher.stop()
        super().tearDown()


class TestLocalKubeconfig(BaseAccessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        deployment = self.root / "src/infra/terraform/deployments/local/deployment.yaml"
        deployment.parent.mkdir(parents=True)
        deployment.write_text(
            json.dumps({
                "public_domain": "example.invalid",
                "clusters": {
                    "ctrl-test": {"role": "ctrl"},
                    "cell-test": {"role": "cell"},
                },
            })
        )
        certificate = self.root / "certificate.pem"
        key = self.root / "key.pem"
        configuration = self.root / "openssl.cnf"
        configuration.write_text(
            "[req]\ndistinguished_name=subject\nx509_extensions=extensions\nprompt=no\n"
            "[subject]\nCN=localhost\n[extensions]\nbasicConstraints=critical,CA:TRUE\n"
            "subjectAltName=IP:127.0.0.1\n"
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-config",
                str(configuration),
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ],
            capture_output=True,
            check=True,
        )
        cert_data = base64.b64encode(certificate.read_bytes()).decode()
        self.source = {
            "clusters": [
                {
                    "cluster": {
                        "certificate-authority-data": cert_data,
                        "server": "https://127.0.0.1:6443",
                        "insecure-skip-tls-verify": True,
                    }
                }
            ],
            "users": [
                {
                    "user": {
                        "client-certificate-data": cert_data,
                        "client-key-data": base64.b64encode(key.read_bytes()).decode(),
                    }
                }
            ],
        }
        self.containers = [
            {
                "Name": f"/floci-aws-fleet-eks-{name}",
                "Id": name,
                "NetworkSettings": {
                    "Ports": {"6443/tcp": [{"HostIp": "0.0.0.0", "HostPort": port}]}
                },
            }
            for name, port in (("ctrl-test", "6517"), ("cell-test", "6503"))
        ]
        self.enterContext(
            mock.patch.object(
                kubeconfig.runtime, "configuration", return_value=mock.Mock(namespace="fleet")
            )
        )
        self.enterContext(
            mock.patch.object(kubeconfig.runtime, "owned_containers", return_value=self.containers)
        )
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        self.commands: list[list[str]] = []

    def run_command(self, arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps(self.source) if arguments[0] == "docker" else "ok", ""
        )

    def test_projects_fresh_contexts_without_overwriting_user_configuration(self) -> None:
        user_config = self.root / "user.yaml"
        user_config.write_text("unrelated user configuration")
        os.environ["KUBECONFIG"] = str(user_config)
        with mock.patch.object(kubeconfig.subprocess, "run", side_effect=self.run_command):
            path = kubeconfig.project(self.root)
        document = json.loads(path.read_text())
        assert document["current-context"] == "ctrl-test"
        assert document["contexts"] == [
            {"name": name, "context": {"cluster": name, "user": name}}
            for name in ("ctrl-test", "cell-test")
        ]
        assert [item["cluster"]["server"] for item in document["clusters"]] == [
            "https://127.0.0.1:6517",
            "https://127.0.0.1:6503",
        ]
        assert all(
            "insecure-skip-tls-verify" not in item["cluster"] for item in document["clusters"]
        )
        assert all(item["cluster"]["certificate-authority-data"] for item in document["clusters"])
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        for index, name in enumerate(("ctrl-test", "cell-test")):
            consumer_path = path.parent / f"{name}.yaml"
            consumer = json.loads(consumer_path.read_text())
            assert consumer == {
                **document,
                "clusters": [document["clusters"][index]],
                "contexts": [document["contexts"][index]],
                "users": [document["users"][index]],
                "current-context": name,
            }
            assert consumer_path.stat().st_mode & 0o777 == 0o600
        assert user_config.read_text() == "unrelated user configuration"
        assert os.environ["KUBECONFIG"] == os.pathsep.join((str(path), str(user_config)))
        kubeconfig.activate(self.root)
        assert os.environ["KUBECONFIG"] == os.pathsep.join((str(path), str(user_config)))
        probes = [command for command in self.commands if command[0] == "kubectl"]
        assert len(probes) == 2
        assert all("--raw=/readyz" in command for command in probes)
        assert self.mock_stdout.getvalue() == ""

    def test_rejects_cluster_names_that_escape_or_replace_shared_config(self) -> None:
        deployment = self.root / "src/infra/terraform/deployments/local/deployment.yaml"
        for name in ("../escaped", "/absolute", "local", "UpperCase", "x" * 64):
            with self.subTest(name=name):
                deployment.write_text(json.dumps({"clusters": {name: {"role": "ctrl"}}}))
                with mock.patch.object(kubeconfig.subprocess, "run") as command:
                    with self.assertRaisesRegex(RuntimeError, "kubeconfig filename"):
                        kubeconfig.project(self.root)
                command.assert_not_called()
                assert not (self.root / ".tmp/kubeconfigs").exists()

    def test_rejects_invalid_tls_material_without_exposing_credentials(self) -> None:
        original = copy.deepcopy(self.source)
        for target, field, value in (
            ("clusters", "certificate-authority-data", None),
            ("clusters", "certificate-authority-data", "secret-canary"),
            ("users", "client-certificate-data", base64.b64encode(b"secret-canary").decode()),
            ("users", "client-key-data", base64.b64encode(b"secret-canary").decode()),
        ):
            with self.subTest(field=field, value=value):
                self.source = copy.deepcopy(original)
                entry = self.source[target][0]["cluster" if target == "clusters" else "user"]
                if value is None:
                    del entry[field]
                else:
                    entry[field] = value
                with mock.patch.object(kubeconfig.subprocess, "run", side_effect=self.run_command):
                    with self.assertRaisesRegex(RuntimeError, "invalid TLS credentials") as error:
                        kubeconfig.project(self.root)
                assert "secret-canary" not in str(error.exception)
                assert not (self.root / ".tmp/kubeconfigs/local.yaml").exists()
        assert self.mock_stdout.getvalue() == ""

    def test_failed_tls_probe_preserves_previous_config_and_environment(self) -> None:
        path = self.root / ".tmp/kubeconfigs/local.yaml"
        path.parent.mkdir(parents=True)
        retained = [path, path.parent / "ctrl-test.yaml", path.parent / "cell-test.yaml"]
        for config in retained:
            config.write_text("retained configuration")
        os.environ["KUBECONFIG"] = "/user/config"
        replies = [
            subprocess.CompletedProcess([], 0, json.dumps(self.source), ""),
            subprocess.CompletedProcess([], 0, json.dumps(self.source), ""),
            subprocess.CompletedProcess([], 0, "ok", ""),
            subprocess.CompletedProcess([], 1, "secret-canary", "secret-canary"),
        ]
        with mock.patch.object(kubeconfig.subprocess, "run", side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, "TLS/readiness check failed") as error:
                kubeconfig.project(self.root)
        assert "secret-canary" not in str(error.exception)
        assert all(config.read_text() == "retained configuration" for config in retained)
        assert os.environ["KUBECONFIG"] == "/user/config"
        assert set(path.parent.iterdir()) == set(retained)


class TestTailnetManager(BaseAccessTestCase):
    """Test TailnetManager gateway forwarding and port resolution."""

    def setUp(self) -> None:
        super().setUp()
        user_directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(Path, "home", return_value=user_directory))

    @staticmethod
    @mock.patch.object(cluster_tailnet.subprocess, "check_output")
    def test_synchronize_cluster_ca_refreshes_existing_certificate(
        mock_check_output: mock.MagicMock,
    ) -> None:
        mock_check_output.return_value = base64.b64encode(b"current CA").decode("ascii")
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            ca_cert = repo_root / ".tmp/state/opentofu/local/pki/public/ca.crt"
            ca_cert.parent.mkdir(parents=True)
            ca_cert.write_bytes(b"stale CA")

            result, changed = cluster_tailnet.synchronize_cluster_ca(
                repo_root,
                "ctrl-eaws-lh1",
            )

            assert result == ca_cert
            assert changed
            assert ca_cert.read_bytes() == b"current CA"
            command = mock_check_output.call_args.args[0]
            assert "cert-manager-system" in command

    @staticmethod
    @mock.patch.object(cluster_tailnet.TailnetManager, "_is_gateway_responsive", return_value=True)
    @mock.patch.object(cluster_tailnet.TailnetManager, "_is_port_open")
    def test_gateway_forward_ignores_an_unmanaged_listener_on_443(
        mock_port_open: mock.MagicMock,
        mock_gateway_responsive: mock.MagicMock,
    ) -> None:
        mock_port_open.return_value = True
        manager = cluster_tailnet.TailnetManager(repo_root=Path("/fake/root"))
        manager.start_gateway_forward()
        assert manager.upstream_port == 18443
        mock_port_open.assert_called_once_with("127.0.0.1", 18443)
        mock_gateway_responsive.assert_called_once_with(18443)

    @staticmethod
    @mock.patch.object(
        cluster_tailnet.shutil,
        "which",
        side_effect=lambda executable: f"/usr/local/bin/{executable}",
    )
    @mock.patch.object(cluster_tailnet.TailnetManager, "_is_port_open", return_value=True)
    def test_install_ssh_config_uses_workspace_serve_port(
        _mock_port_open: mock.MagicMock,
        _mock_which: mock.MagicMock,
    ) -> None:
        manager = cluster_tailnet.TailnetManager(
            repo_root=Path("/fake/root"),
            public_domain="example.invalid",
        )

        manager.install_ssh_config()

        config = manager.ssh_config.read_text(encoding="utf-8")
        assert "Host *.*.c.example.invalid\n" in config
        assert "  Port 2222\n" in config
        assert (
            "  ProxyCommand /usr/local/bin/python3 "
            f"{manager.ssh_proxy} --tailscale /usr/local/bin/tailscale "
            f"--socket {manager.socket_path} %h %p\n" in config
        )
        assert manager.ssh_proxy.read_bytes() == Path(workspace_ssh_proxy.__file__).read_bytes()
        assert manager.ssh_proxy.stat().st_mode & 511 == 448

    @staticmethod
    @mock.patch.object(cluster_tailnet.subprocess, "Popen")
    @mock.patch.object(cluster_tailnet.TailnetManager, "_is_port_open")
    def test_start_gateway_forward(
        mock_port_open: mock.MagicMock, mock_popen: mock.MagicMock
    ) -> None:
        mock_port_open.side_effect = [False, True]
        mock_proc = mock.Mock(pid=12345)
        mock_popen.return_value = mock_proc

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = cluster_tailnet.TailnetManager(repo_root=Path("/fake/root"))
            manager.bridge_state_dir = Path(temp_dir)
            manager.gateway_forward_pid_file = manager.bridge_state_dir / "gateway-forward.pid"
            manager.gateway_forward_log_file = manager.bridge_state_dir / "gateway-forward.log"

            manager.start_gateway_forward()
            assert manager.upstream_port == 18443
            assert manager.gateway_forward_pid_file.is_file()
            assert manager.gateway_forward_pid_file.read_text(encoding="utf-8").strip() == "12345"
            mock_popen.assert_called_once()

    @staticmethod
    @mock.patch.object(cluster_tailnet.subprocess, "Popen")
    @mock.patch.object(cluster_tailnet.TailnetManager, "_stop_port_listeners")
    @mock.patch.object(cluster_tailnet.TailnetManager, "_is_gateway_responsive", return_value=False)
    @mock.patch.object(cluster_tailnet.TailnetManager, "_is_port_open")
    def test_start_gateway_forward_recovers_stale_listener(
        mock_port_open: mock.MagicMock,
        mock_gateway_responsive: mock.MagicMock,
        mock_stop_listeners: mock.MagicMock,
        mock_popen: mock.MagicMock,
    ) -> None:
        mock_port_open.side_effect = [True, True]
        mock_proc = mock.Mock(pid=55555)
        mock_popen.return_value = mock_proc

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = cluster_tailnet.TailnetManager(repo_root=Path("/fake/root"))
            manager.bridge_state_dir = Path(temp_dir)
            manager.gateway_forward_pid_file = manager.bridge_state_dir / "gateway-forward.pid"
            manager.gateway_forward_log_file = manager.bridge_state_dir / "gateway-forward.log"

            manager.start_gateway_forward()
            assert manager.upstream_port == 18443
            mock_gateway_responsive.assert_called_once_with(18443)
            mock_stop_listeners.assert_called_once_with(18443)
            mock_popen.assert_called_once()
            assert manager.gateway_forward_pid_file.read_text(encoding="utf-8").strip() == "55555"

    @staticmethod
    @mock.patch.object(cluster_tailnet.TailnetManager, "_is_port_open", return_value=False)
    @mock.patch.object(cluster_tailnet.TailnetManager, "stop_process_group")
    def test_down_cleans_up_gateway_forward(
        mock_stop_pg: mock.MagicMock,
        _mock_is_port_open: mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = cluster_tailnet.TailnetManager(repo_root=Path("/fake/root"))
            manager.bridge_state_dir = Path(temp_dir)
            manager.gateway_forward_pid_file = manager.bridge_state_dir / "gateway-forward.pid"
            manager.gateway_forward_pid_file.write_text("54321\n", encoding="utf-8")

            with mock.patch.object(cluster_tailnet.shutil, "which", return_value=None):
                manager.down()

            mock_stop_pg.assert_called_once_with(54321)
            assert not manager.gateway_forward_pid_file.is_file()

    @staticmethod
    @mock.patch.object(cluster_tailnet.subprocess, "Popen")
    @mock.patch.object(cluster_tailnet.TailnetManager, "start_bridges")
    @mock.patch.object(cluster_tailnet.TailnetManager, "is_daemon_running", return_value=False)
    @mock.patch.object(cluster_tailnet.TailnetManager, "stop_process_group")
    def test_start_daemon_cleans_stale_pid_and_passes_session(
        mock_stop_pg: mock.MagicMock,
        _mock_is_running: mock.MagicMock,
        _mock_start_bridges: mock.MagicMock,
        mock_popen: mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            manager = cluster_tailnet.TailnetManager(repo_root=Path("/fake/root"))
            manager.state_dir = temp_path
            manager.pid_file = temp_path / "tailscaled.pid"
            manager.socket_path = temp_path / "tailscaled.sock"
            manager.log_file = temp_path / "tailscaled.log"
            manager.pid_file.write_text("99999\n", encoding="utf-8")

            mock_proc = mock.Mock(pid=11111)
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            with (
                mock.patch.object(
                    cluster_tailnet.shutil, "which", return_value="/usr/bin/tailscaled"
                ),
                mock.patch.object(
                    cluster_tailnet.TailnetManager, "_is_port_open", return_value=False
                ),
                mock.patch.object(Path, "is_socket", return_value=True),
            ):
                manager.start_daemon()

            mock_stop_pg.assert_called_once_with(99999)
            mock_popen.assert_called_once()
            _, kwargs = mock_popen.call_args
            assert kwargs.get("start_new_session")
            assert manager.pid_file.read_text(encoding="utf-8").strip() == "11111"

    @mock.patch.object(cluster_tailnet.subprocess, "Popen")
    @mock.patch.object(cluster_tailnet.TailnetManager, "start_bridges")
    @mock.patch.object(cluster_tailnet.TailnetManager, "is_daemon_running", return_value=False)
    def test_start_daemon_detects_crash(
        self,
        _mock_is_running: mock.MagicMock,
        _mock_start_bridges: mock.MagicMock,
        mock_popen: mock.MagicMock,
    ) -> None:
        mock_proc = mock.Mock(pid=11111, returncode=1)
        mock_proc.poll.return_value = 1
        mock_popen.return_value = mock_proc
        manager = cluster_tailnet.TailnetManager(repo_root=Path("/fake/root"))

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(
                cluster_tailnet.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0),
            ),
        ):
            manager.bridge_state_dir = Path(temp_dir)
            manager.pid_file = manager.bridge_state_dir / "tailscaled.pid"
            manager.log_file = manager.bridge_state_dir / "tailscaled.log"

            with (
                mock.patch.object(
                    cluster_tailnet.shutil, "which", return_value="/usr/bin/tailscaled"
                ),
                mock.patch.object(
                    cluster_tailnet.TailnetManager, "_is_port_open", return_value=False
                ),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.start_daemon()
                assert "exited unexpectedly with code 1" in str(ctx.exception)

    @staticmethod
    @mock.patch.object(cluster_tailnet.webbrowser, "open")
    @mock.patch.object(cluster_tailnet.subprocess, "Popen")
    @mock.patch.object(cluster_tailnet.TailnetManager, "install_ssh_config")
    @mock.patch.object(cluster_tailnet.TailnetManager, "start_daemon")
    def test_up_opens_browser_on_auth_url(
        _mock_start_daemon: mock.MagicMock,
        _mock_install_ssh: mock.MagicMock,
        mock_popen: mock.MagicMock,
        mock_browser_open: mock.MagicMock,
    ) -> None:
        manager = cluster_tailnet.TailnetManager(repo_root=Path("/fake/root"))
        mock_proc = mock.MagicMock()
        mock_proc.stdout = [
            "To authenticate, visit:\n",
            "\n",
            "\thttps://headscale.ctrl-eaws-lh1.c.corp.local.internal/register/hskey-authreq-xyz\n",
            "\n",
            "Success.\n",
        ]
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        callback = mock.MagicMock()

        with mock.patch.object(cluster_tailnet.shutil, "which", return_value="/usr/bin/tailscale"):
            manager.up(on_auth_url=callback)

        expected_url = (
            "https://headscale.ctrl-eaws-lh1.c.corp.local.internal/register/hskey-authreq-xyz"
        )
        mock_browser_open.assert_called_once_with(expected_url)
        callback.assert_called_once_with(expected_url)


class TestBrowserProxy(BaseAccessTestCase):
    """Test BrowserProxy arguments and direct gateway fallback."""

    @staticmethod
    def test_arguments_with_direct_gateway() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            parsed = browser_proxy.arguments([
                "--listen-address",
                "127.0.0.1:0",
                "--socks-address",
                "127.0.0.1:1055",
                "--direct-gateway",
                "127.0.0.1:18443",
                "--direct-gateway-target",
                "172.31.0.11",
                "--map",
                "*.unit.test=172.31.0.11",
                "--pac-file",
                str(temp_path / "proxy.pac"),
                "--ready-file",
                str(temp_path / "proxy.ready"),
            ])
            assert parsed.direct_gateway == ("127.0.0.1", 18443)
            assert parsed.direct_gateway_target == ipaddress.IPv4Address("172.31.0.11")
            assert parsed.socks_address == ("127.0.0.1", 1055)

    @staticmethod
    def test_target_address_prefers_specific_wildcard() -> None:
        proxy = browser_proxy.BrowserProxy.__new__(browser_proxy.BrowserProxy)
        proxy.mappings = {
            "*.c.unit.test": ipaddress.IPv4Address("172.31.0.11"),
            "*.cell-eaws-lh1.c.unit.test": ipaddress.IPv4Address("172.31.16.11"),
        }
        assert proxy.target_address("velero-ui.cell-eaws-lh1.c.unit.test") == ipaddress.IPv4Address(
            "172.31.16.11"
        )
        assert proxy.target_address("argocd.ctrl-eaws-lh1.c.unit.test") == ipaddress.IPv4Address(
            "172.31.0.11"
        )


class TestClusterBrowser(BaseAccessTestCase):
    """Test cluster browser helper functions."""

    @mock.patch.object(cluster_browser, "TailnetManager")
    def test_browser_setup_propagates_a_failed_managed_gateway(
        self,
        manager_type: mock.MagicMock,
    ) -> None:
        manager = manager_type.return_value
        manager.start_gateway_forward.side_effect = TimeoutError("gateway unavailable")
        with self.assertRaises(TimeoutError):
            cluster_browser._ensure_tailnet(Path("/fake/root"), "ctrl-test", "example.invalid")
        manager.is_daemon_running.assert_not_called()

    @staticmethod
    @mock.patch.object(cluster_browser, "compute_ca_spki_pin", return_value="pin-ca")
    @mock.patch.object(cluster_browser, "resolve_gateway_spki_pins", return_value=["pin-gw"])
    @mock.patch.object(cluster_browser, "get_spki_pin_openssl", return_value="pin-leaf")
    def test_compute_browser_spki_list(
        mock_get_leaf: mock.MagicMock,
        mock_gw_pins: mock.MagicMock,
        mock_ca_pin: mock.MagicMock,
    ) -> None:
        spki_list = cluster_browser.compute_browser_spki_list(
            ca_cert=Path("/fake/ca.crt"),
            clusters=["ctrl-eaws-lh1", "cell-eaws-lh1"],
            private_control_domain="ctrl-eaws-lh1.c.unit.test",
            public_domain="unit.test",
            upstream_port=18443,
        )
        assert "pin-ca" in spki_list
        assert "pin-gw" in spki_list
        assert "pin-leaf" in spki_list
        mock_get_leaf.assert_any_call("127.0.0.1:18443", "headlamp.ctrl-eaws-lh1.c.unit.test")
        mock_get_leaf.assert_any_call("127.0.0.1:18443", "headlamp.unit.test")

    @staticmethod
    @mock.patch("subprocess.Popen")
    def test_start_browser_proxy(mock_popen: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ready_file = temp_path / "proxy.ready"
            ready_file.write_text("127.0.0.1:18081\n", encoding="utf-8")

            mock_proc = mock.MagicMock()
            mock_popen.return_value = mock_proc

            proc, address = cluster_browser.start_browser_proxy(
                temp_path=temp_path,
                socks_address="127.0.0.1:1055",
                domains=["ctrl-eaws-lh1.c.unit.test", "unit.test"],
                gateway_ip="172.31.0.11",
                direct_gateway="127.0.0.1:18443",
            )
            assert proc == mock_proc
            assert address == "127.0.0.1:18081"
            pac_content = (temp_path / "proxy.pac").read_text(encoding="utf-8")
            assert "unit.test" in pac_content
            assert "PROXY 127.0.0.1:18081" in pac_content
            mock_popen.assert_called_once()

    @staticmethod
    @mock.patch("subprocess.Popen")
    def test_start_browser_proxy_with_mappings(mock_popen: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ready_file = temp_path / "proxy.ready"
            ready_file.write_text("127.0.0.1:18082\n", encoding="utf-8")

            mock_proc = mock.MagicMock()
            mock_popen.return_value = mock_proc

            proc, address = cluster_browser.start_browser_proxy(
                temp_path=temp_path,
                socks_address="127.0.0.1:1055",
                domains=[
                    "c.unit.test",
                    "*.cell-eaws-lh1.c.unit.test=172.31.16.11",
                ],
                gateway_ip="172.31.0.11",
                direct_gateway="127.0.0.1:18443",
            )
            assert proc == mock_proc
            assert address == "127.0.0.1:18082"
            mock_popen.assert_called_once()
            called_cmd = mock_popen.call_args[0][0]
            assert "--map" in called_cmd
            assert "*.cell-eaws-lh1.c.unit.test=172.31.16.11" in called_cmd


class WorkspaceSSHProxyTest(unittest.TestCase):
    """Test canonical workspace SSH alias resolution."""

    @staticmethod
    @mock.patch.object(workspace_ssh_proxy.subprocess, "run")
    def test_resolves_exactly_one_tailnet_address(run: mock.MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "ResponseCode": "RCodeSuccess",
                "Answers": [{"Type": "TypeA", "Body": "100.64.12.34"}],
            }),
        )

        address = workspace_ssh_proxy.resolve_tailnet_ipv4(
            "/usr/bin/tailscale",
            Path("/run/tailscaled.sock"),
            "workspace.cell.tailnet.c.example.invalid",
        )

        assert address == "100.64.12.34"
        run.assert_called_once_with(
            [
                "/usr/bin/tailscale",
                "--socket=/run/tailscaled.sock",
                "dns",
                "query",
                "--json",
                "workspace.cell.tailnet.c.example.invalid",
                "A",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )

    @mock.patch.object(workspace_ssh_proxy.subprocess, "run")
    def test_rejects_address_outside_tailnet(self, run: mock.MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "ResponseCode": "RCodeSuccess",
                "Answers": [{"Type": "TypeA", "Body": "203.0.113.10"}],
            }),
        )

        with self.assertRaisesRegex(RuntimeError, r"outside 100\.64\.0\.0/10, 172\.16\.0\.0/12"):
            workspace_ssh_proxy.resolve_tailnet_ipv4(
                "/usr/bin/tailscale",
                Path("/run/tailscaled.sock"),
                "workspace.cell.tailnet.c.example.invalid",
            )

    @staticmethod
    @mock.patch.object(workspace_ssh_proxy.subprocess, "run")
    def test_resolves_cluster_network_address(run: mock.MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "ResponseCode": "RCodeSuccess",
                "Answers": [{"Type": "TypeA", "Body": "172.31.27.43"}],
            }),
        )

        address = workspace_ssh_proxy.resolve_tailnet_ipv4(
            "/usr/bin/tailscale",
            Path("/run/tailscaled.sock"),
            "workspace.cell.tailnet.c.example.invalid",
        )

        assert address == "172.31.27.43"

    @staticmethod
    @mock.patch.object(workspace_ssh_proxy.subprocess, "run")
    def test_connects_resolved_address_with_tailnet_client(run: mock.MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

        result = workspace_ssh_proxy.connect_tailnet(
            "/usr/bin/tailscale",
            Path("/run/tailscaled.sock"),
            "100.64.12.34",
            2222,
        )

        assert result == 0
        run.assert_called_once_with(
            [
                "/usr/bin/tailscale",
                "--socket=/run/tailscaled.sock",
                "nc",
                "100.64.12.34",
                "2222",
            ],
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
