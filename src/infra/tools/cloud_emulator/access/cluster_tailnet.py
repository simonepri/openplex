#!/usr/bin/env python3
"""Manages isolated userspace Tailscale client and Headscale loopback bridges for local mesh routing."""

from __future__ import annotations

import argparse
import base64
import contextlib
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from infra.terraform.lifecycle.cluster_common import (
    find_repo_root,
    get_intranet_domain,
    get_public_domain,
)

from infra.tools.cloud_emulator.access import kubeconfig, workspace_ssh_proxy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_SOCKS_PORT = 1055
DEFAULT_BRIDGE_PORT = 18080
DEFAULT_DERP_PORT = 3340
DEFAULT_GATEWAY_FORWARD_PORT = 18443
DEFAULT_INSTALLATION_NAME = "openplex"
DEFAULT_CONTROL_CLUSTER = "ctrl-eaws-lh1"
CERT_MANAGER_NAMESPACE = "cert-manager-system"
WORKSPACE_SSH_PORT = 2222


def synchronize_cluster_ca(repo_root: Path, control_cluster: str) -> tuple[Path, bool]:
    """Write the current cluster-local CA to the local access state."""
    ca_cert = repo_root / ".tmp/state/opentofu/local/pki/public/ca.crt"
    ca_b64 = subprocess.check_output(
        [
            "kubectl",
            "--context",
            control_cluster,
            "get",
            "secret",
            "cluster-local-ca",
            "-n",
            CERT_MANAGER_NAMESPACE,
            "-o",
            "jsonpath={.data.tls\\.crt}",
        ],
        text=True,
        timeout=5,
    ).strip()
    if not ca_b64:
        raise RuntimeError("cluster-local-ca Secret contains no TLS certificate")

    ca_bytes = base64.b64decode(ca_b64, validate=True)
    changed = not ca_cert.is_file() or ca_cert.read_bytes() != ca_bytes
    if changed:
        ca_cert.parent.mkdir(parents=True, exist_ok=True)
        temporary_cert = ca_cert.with_suffix(".crt.tmp")
        temporary_cert.write_bytes(ca_bytes)
        temporary_cert.replace(ca_cert)
    return ca_cert, changed


class TailnetManager:
    """Manages the lifecycle of the isolated userspace Tailscale daemon."""

    def __init__(
        self,
        repo_root: Path | None = None,
        installation_name: str = DEFAULT_INSTALLATION_NAME,
        public_domain: str | None = None,
        intranet_domain: str | None = None,
        cluster_domain: str | None = None,
        control_cluster: str = DEFAULT_CONTROL_CLUSTER,
        socks_port: int = DEFAULT_SOCKS_PORT,
    ) -> None:
        self.repo_root = repo_root or find_repo_root()
        kubeconfig.activate(self.repo_root)
        self.installation_name = installation_name
        self.public_domain = public_domain or get_public_domain(self.repo_root)
        self.intranet_domain = intranet_domain or (
            public_domain
            if public_domain and public_domain != "local.internal"
            else get_intranet_domain(self.repo_root)
        )
        self.control_cluster = control_cluster
        self.socks_port = socks_port
        self.socks_address = f"127.0.0.1:{socks_port}"
        self.upstream_port = DEFAULT_GATEWAY_FORWARD_PORT

        self.cluster_domain = cluster_domain or f"c.{self.intranet_domain}"
        self.access_alias_domain = self.cluster_domain
        self.state_dir = Path.home() / f".local/state/{installation_name}-tailnet"
        self.bridge_state_dir = Path.home() / f".local/state/{installation_name}-tailnet-bridge"
        self.socket_path = self.state_dir / "tailscaled.sock"
        self.state_file = self.state_dir / "tailscaled.state"
        self.pid_file = self.state_dir / "tailscaled.pid"
        self.log_file = self.state_dir / "tailscaled.log"
        self.ssh_proxy = self.state_dir / "workspace-ssh-proxy"

        self.ca_cert = self.repo_root / ".tmp/state/opentofu/local/pki/public/ca.crt"
        self.ssh_config = Path.home() / ".ssh/config"

        # Bridge files
        self.bridge_pid_file = self.bridge_state_dir / "headscale-bridge.pid"
        self.bridge_ready_file = self.bridge_state_dir / "headscale-bridge.ready"
        self.bridge_log_file = self.bridge_state_dir / "headscale-bridge.log"

        self.gateway_forward_pid_file = self.bridge_state_dir / "gateway-forward.pid"
        self.gateway_forward_log_file = self.bridge_state_dir / "gateway-forward.log"

        self.derp_bridge_pid_file = self.bridge_state_dir / "headscale-derp-bridge.pid"
        self.derp_bridge_ready_file = self.bridge_state_dir / "headscale-derp-bridge.ready"
        self.derp_bridge_log_file = self.bridge_state_dir / "headscale-derp-bridge.log"
        self.derp_bridge_cert = self.bridge_state_dir / "headscale-derp-bridge.crt"
        self.derp_bridge_key = self.bridge_state_dir / "headscale-derp-bridge.key"

        self.client_hostname = f"{os.environ.get('USER', 'developer')}-{installation_name}-client"

    def is_daemon_running(self) -> bool:
        if not self.pid_file.is_file():
            return False
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return False
        if not self.is_pid_alive(pid):
            return False
        return self.socket_path.is_socket()

    def is_bridge_running(
        self,
        pid_file: Path,
        ready_file: Path,
        expected_upstream_host: str | None = None,
    ) -> bool:
        if not pid_file.is_file() or not ready_file.is_file():
            return False
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return False
        if not self.is_pid_alive(pid):
            return False
        if expected_upstream_host is not None:
            try:
                content = ready_file.read_text(encoding="utf-8").strip()
                if content != expected_upstream_host:
                    return False
            except OSError:
                return False
        return True

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            return probe.connect_ex((host, port)) == 0

    def _stop_port_listeners(self, port: int) -> None:
        try:
            output = subprocess.check_output(
                ["lsof", "-t", "-i", f":{port}"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            return
        if not output:
            return
        for line in output.splitlines():
            try:
                orphan_pid = int(line.strip())
                self.stop_process_group(orphan_pid)
            except ValueError:
                pass
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._is_port_open("127.0.0.1", port):
                return
            time.sleep(0.1)

    @staticmethod
    def is_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _stop_pid(self, pid_file: Path, timeout: float = 5.0) -> None:
        if not pid_file.is_file():
            return
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)
            return
        try:
            os.kill(pid, 15)  # SIGTERM
        except (OSError, ProcessLookupError):
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except (OSError, ProcessLookupError):
                return
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, 9)  # SIGKILL

    @staticmethod
    def stop_process_group(pid: int, timeout: float = 5.0) -> None:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, 15)  # SIGTERM
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, 15)
            except (OSError, ProcessLookupError):
                return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except (OSError, ProcessLookupError):
                return
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, 9)  # SIGKILL
        except (OSError, ProcessLookupError):
            with contextlib.suppress(OSError, ProcessLookupError):
                os.kill(pid, 9)

    def _is_gateway_responsive(self, port: int, timeout: float = 1.0) -> bool:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sni_host = f"home.{self.access_alias_domain}"
            with (
                socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock,
                ctx.wrap_socket(sock, server_hostname=sni_host) as ssock,
            ):
                return ssock.cipher() is not None
        except Exception:
            return False

    def start_gateway_forward(self) -> None:
        if self._is_port_open("127.0.0.1", self.upstream_port):
            if self._is_gateway_responsive(self.upstream_port):
                return
            self._stop_port_listeners(self.upstream_port)

        self.bridge_state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.gateway_forward_pid_file.is_file():
            try:
                pid = int(self.gateway_forward_pid_file.read_text(encoding="utf-8").strip())
                self.stop_process_group(pid)
            except (ValueError, OSError):
                pass
            self.gateway_forward_pid_file.unlink(missing_ok=True)
        cmd = [
            "sh",
            "-c",
            (
                f"while true; do kubectl --context {self.control_cluster} "
                f"-n envoy-gateway-system port-forward --address 127.0.0.1,::1 svc/private-access-gateway "
                f"{self.upstream_port}:443; sleep 1; done"
            ),
        ]
        with self.gateway_forward_log_file.open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
        self.gateway_forward_pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._is_port_open("127.0.0.1", self.upstream_port):
                return
            time.sleep(0.1)
        raise TimeoutError(f"Gateway port-forward failed to listen on port {self.upstream_port}")

    def start_bridges(self) -> None:
        self.bridge_state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ca_cert, ca_changed = synchronize_cluster_ca(
            self.repo_root,
            self.control_cluster,
        )
        if ca_changed:
            self._stop_pid_file(self.bridge_pid_file)
            self.bridge_ready_file.unlink(missing_ok=True)
            self._stop_pid_file(self.derp_bridge_pid_file)
            self.derp_bridge_ready_file.unlink(missing_ok=True)
        self.start_gateway_forward()
        python_bin = sys.executable
        bridge_script = Path(__file__).with_name("tls_bridge.py")

        if not self.ca_cert.is_file():
            raise FileNotFoundError(f"Local CA certificate not found: {self.ca_cert}")

        upstream_host = f"headscale.{self.control_cluster}.{self.access_alias_domain}"

        headscale_port = 8443 if self._is_port_open("127.0.0.1", 8443) else self.upstream_port

        # 1. HTTP Bridge
        if not self.is_bridge_running(self.bridge_pid_file, self.bridge_ready_file, upstream_host):
            if self.bridge_pid_file.is_file():
                try:
                    pid = int(self.bridge_pid_file.read_text(encoding="utf-8").strip())
                    self.stop_process_group(pid)
                except (ValueError, OSError):
                    pass
            self.bridge_pid_file.unlink(missing_ok=True)
            self.bridge_ready_file.unlink(missing_ok=True)
            cmd = [
                python_bin,
                str(bridge_script),
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(DEFAULT_BRIDGE_PORT),
                "--upstream-address",
                "127.0.0.1",
                "--upstream-host",
                upstream_host,
                "--upstream-port",
                str(headscale_port),
                "--ca-file",
                str(self.ca_cert),
                "--ready-file",
                str(self.bridge_ready_file),
            ]
            with self.bridge_log_file.open("a", encoding="utf-8") as log:
                proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
            self.bridge_pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
            self._wait_for_file(
                self.bridge_ready_file,
                timeout=10.0,
                desc="Headscale HTTP bridge",
                proc=proc,
            )

        # 2. DERP HTTPS Bridge
        if not self.is_bridge_running(
            self.derp_bridge_pid_file, self.derp_bridge_ready_file, upstream_host
        ):
            if self.derp_bridge_pid_file.is_file():
                try:
                    pid = int(self.derp_bridge_pid_file.read_text(encoding="utf-8").strip())
                    self.stop_process_group(pid)
                except (ValueError, OSError):
                    pass
            self.derp_bridge_pid_file.unlink(missing_ok=True)
            self.derp_bridge_ready_file.unlink(missing_ok=True)
            if not self._is_derp_cert_valid():
                self._generate_derp_cert()

            cmd = [
                python_bin,
                str(bridge_script),
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(DEFAULT_DERP_PORT),
                "--upstream-address",
                "127.0.0.1",
                "--upstream-host",
                upstream_host,
                "--upstream-port",
                str(headscale_port),
                "--ca-file",
                str(self.ca_cert),
                "--listen-cert-file",
                str(self.derp_bridge_cert),
                "--listen-key-file",
                str(self.derp_bridge_key),
                "--ready-file",
                str(self.derp_bridge_ready_file),
            ]
            with self.derp_bridge_log_file.open("a", encoding="utf-8") as log:
                proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
            self.derp_bridge_pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
            self._wait_for_file(
                self.derp_bridge_ready_file,
                timeout=10.0,
                desc="Headscale DERP bridge",
                proc=proc,
            )

    def _is_derp_cert_valid(self) -> bool:
        if not self.derp_bridge_cert.is_file() or not self.derp_bridge_key.is_file():
            return False
        res = subprocess.run(
            ["openssl", "x509", "-checkend", "86400", "-noout", "-in", str(self.derp_bridge_cert)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return res.returncode == 0

    def _generate_derp_cert(self) -> None:
        cmd = [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "365",
            "-subj",
            "/CN=cluster-tailnet-loopback",
            "-keyout",
            str(self.derp_bridge_key),
            "-out",
            str(self.derp_bridge_cert),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.derp_bridge_cert.chmod(0o600)
        self.derp_bridge_key.chmod(0o600)

    def _wait_for_file(
        self,
        path: Path,
        timeout: float,
        desc: str,
        proc: subprocess.Popen[bytes] | None = None,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"{desc} process exited unexpectedly with code {proc.returncode}"
                )
            if path.is_file() and path.stat().st_size > 0:
                return
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for {desc} at {path}")

    def start_daemon(self) -> None:
        self.start_bridges()
        if self.is_daemon_running():
            return

        if self.pid_file.is_file():
            try:
                pid = int(self.pid_file.read_text(encoding="utf-8").strip())
                self.stop_process_group(pid)
            except (ValueError, OSError):
                pass
            self.pid_file.unlink(missing_ok=True)

        if self._is_port_open("127.0.0.1", self.socks_port):
            self._stop_port_listeners(self.socks_port)

        tailscaled_bin = shutil.which("tailscaled")
        if not tailscaled_bin:
            raise FileNotFoundError("tailscaled executable not found in PATH")

        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.pid_file.unlink(missing_ok=True)

        env = os.environ.copy()
        env["TS_DEBUG_ALWAYS_USE_DERP"] = "true"

        cmd = [
            tailscaled_bin,
            "--tun=userspace-networking",
            f"--state={self.state_file}",
            f"--socket={self.socket_path}",
            f"--socks5-server={self.socks_address}",
            "--no-logs-no-support",
        ]
        with self.log_file.open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=log, start_new_session=True)

        self.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")

        # Wait for socket
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"tailscaled exited unexpectedly with code {proc.returncode}. "
                    f"Check log file at {self.log_file}"
                )
            if self.socket_path.is_socket():
                return
            time.sleep(0.1)
        raise TimeoutError(f"tailscaled failed to create socket at {self.socket_path}")

    def _get_local_auth_key(self) -> str | None:
        auth_key = os.environ.get("HEADSCALE_PREAUTH_KEY") or os.environ.get("TAILSCALE_AUTH_KEY")
        if auth_key:
            return auth_key
        commands = []
        for user in ("local", "1"):
            commands.extend([
                [
                    "kubectl",
                    "--context",
                    self.control_cluster,
                    "exec",
                    "-n",
                    "headscale",
                    "deploy/headscale",
                    "-c",
                    "headscale",
                    "--",
                    "headscale",
                    "preauthkeys",
                    "create",
                    "--user",
                    user,
                    "--reusable",
                    "--expiration",
                    "24h",
                ],
                [
                    "docker",
                    "exec",
                    "openplex-local-headscale",
                    "headscale",
                    "preauthkeys",
                    "create",
                    "--user",
                    user,
                    "--reusable",
                    "--expiration",
                    "24h",
                ],
            ])
        for cmd in commands:
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    for line in reversed(res.stdout.strip().splitlines()):
                        candidate = line.strip()
                        if candidate.startswith("hskey-auth-"):
                            return candidate
            except Exception:
                pass
        return None

    def _auto_register_node(self, url: str) -> bool:
        match_key = re.search(r"/register/([a-zA-Z0-9_-]+)", url)
        if not match_key:
            return False
        node_key = match_key.group(1)
        commands = []
        for user in ("local", "1"):
            commands.extend([
                [
                    "kubectl",
                    "--context",
                    self.control_cluster,
                    "exec",
                    "-n",
                    "headscale",
                    "deploy/headscale",
                    "-c",
                    "headscale",
                    "--",
                    "headscale",
                    "nodes",
                    "register",
                    "--key",
                    node_key,
                    "--user",
                    user,
                ],
                [
                    "docker",
                    "exec",
                    "openplex-local-headscale",
                    "headscale",
                    "nodes",
                    "register",
                    "--key",
                    node_key,
                    "--user",
                    user,
                ],
            ])
        for cmd in commands:
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    def _handle_auth_stream(
        self,
        proc: subprocess.Popen[str],
        on_auth_url: Callable[[str], None] | None,
    ) -> None:
        url_pattern = re.compile(r"https?://\S+")
        opened_urls: set[str] = set()

        def handle_url(url: str) -> None:
            if url in opened_urls:
                return
            opened_urls.add(url)
            if self._auto_register_node(url):
                return
            with contextlib.suppress(Exception):
                webbrowser.open(url)
            if on_auth_url is not None:
                with contextlib.suppress(Exception):
                    on_auth_url(url)

        saw_auth = False
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if "To authenticate, visit:" in line:
                saw_auth = True
                m = url_pattern.search(line)
                if m:
                    handle_url(m.group(0))
                    saw_auth = False
                continue
            if saw_auth or "register/" in line:
                m = url_pattern.search(line)
                if m:
                    handle_url(m.group(0))
                    saw_auth = False

    def up(self, on_auth_url: Callable[[str], None] | None = None) -> None:
        self.start_daemon()
        tailscale_bin = shutil.which("tailscale")
        if not tailscale_bin:
            raise FileNotFoundError("tailscale executable not found in PATH")

        login_server = f"http://127.0.0.1:{DEFAULT_BRIDGE_PORT}"

        cmd = [
            tailscale_bin,
            f"--socket={self.socket_path}",
            "up",
            "--reset",
            f"--login-server={login_server}",
            "--accept-dns=true",
            "--accept-routes=true",
            f"--hostname={self.client_hostname}",
            "--ssh=false",
        ]
        auth_key = self._get_local_auth_key()
        if auth_key:
            cmd.append(f"--authkey={auth_key}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._handle_auth_stream(proc, on_auth_url)
        returncode = proc.wait()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd)

        self.install_ssh_config()

    def _stop_pid_file(self, pid_file: Path) -> None:
        if pid_file.is_file():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                self.stop_process_group(pid)
            except (ValueError, OSError):
                pass
            pid_file.unlink(missing_ok=True)

    def _stop_active_ports(self) -> None:
        ports = [DEFAULT_BRIDGE_PORT, DEFAULT_DERP_PORT, self.socks_port, self.upstream_port]
        for port in ports:
            if self._is_port_open("127.0.0.1", port):
                self._stop_port_listeners(port)

    def down(self) -> None:
        tailscale_bin = shutil.which("tailscale")
        if tailscale_bin and self.socket_path.is_socket():
            subprocess.run(
                [tailscale_bin, f"--socket={self.socket_path}", "down"],
                capture_output=True,
                check=False,
            )

        self._stop_pid_file(self.pid_file)
        self.socket_path.unlink(missing_ok=True)

        self._stop_pid_file(self.bridge_pid_file)
        self.bridge_ready_file.unlink(missing_ok=True)

        self._stop_pid_file(self.derp_bridge_pid_file)
        self.derp_bridge_ready_file.unlink(missing_ok=True)

        self._stop_pid_file(self.gateway_forward_pid_file)
        self._stop_active_ports()

    def status(self) -> int:
        if not self.is_daemon_running():
            return 1
        tailscale_bin = shutil.which("tailscale")
        if not tailscale_bin:
            return 1
        return subprocess.run([tailscale_bin, f"--socket={self.socket_path}", "status"]).returncode

    def install_ssh_config(self) -> None:
        self.ssh_config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_proxy = self.ssh_proxy.with_name(f".{self.ssh_proxy.name}.{os.getpid()}.tmp")
        try:
            temporary_proxy.write_bytes(Path(workspace_ssh_proxy.__file__).read_bytes())
            temporary_proxy.chmod(0o700)
            temporary_proxy.replace(self.ssh_proxy)
        finally:
            temporary_proxy.unlink(missing_ok=True)

        python_bin = shutil.which("python3")
        tailscale_bin = shutil.which("tailscale")
        if not python_bin or not tailscale_bin:
            raise FileNotFoundError("python3 and tailscale executables must be available in PATH")
        proxy_cmd = shlex.join([
            python_bin,
            str(self.ssh_proxy),
            "--tailscale",
            tailscale_bin,
            "--socket",
            str(self.socket_path),
            "%h",
            "%p",
        ])
        managed_block = (
            f"# BEGIN CLUSTER TAILNET\n"
            f"Host *.*.{self.access_alias_domain}\n"
            f"  IgnoreUnknown UseKeychain\n"
            f"  AddKeysToAgent yes\n"
            f"  UseKeychain yes\n"
            f"  IdentitiesOnly yes\n"
            f"  IdentityFile ~/.ssh/{self.access_alias_domain}_ed25519\n"
            f"  IdentityFile {self.state_dir}/workspace_ed25519\n"
            f"  Port {WORKSPACE_SSH_PORT}\n"
            f"  ProxyCommand {proxy_cmd}\n"
            f"# END CLUSTER TAILNET\n"
        )
        existing = self.ssh_config.read_text(encoding="utf-8") if self.ssh_config.is_file() else ""
        cleaned = re.sub(
            r"# BEGIN [A-Z]+ TAILNET.*?# END [A-Z]+ TAILNET\n?",
            "",
            existing,
            flags=re.DOTALL,
        )
        new_content = managed_block + cleaned
        self.ssh_config.write_text(new_content, encoding="utf-8")
        self.ssh_config.chmod(0o600)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage isolated userspace Tailscale mesh.")
    parser.add_argument("action", choices=("start", "up", "down", "status"))
    parser.add_argument("--socks-port", type=int, default=DEFAULT_SOCKS_PORT)
    args = parser.parse_args(argv)

    manager = TailnetManager(socks_port=args.socks_port)
    if args.action == "start":
        manager.start_daemon()
        manager.install_ssh_config()
    elif args.action == "up":
        manager.up()
    elif args.action == "down":
        manager.down()
    elif args.action == "status":
        return manager.status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
