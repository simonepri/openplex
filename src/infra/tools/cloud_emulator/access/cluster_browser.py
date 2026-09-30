#!/usr/bin/env python3
"""Launches Chromium in an isolated profile with SPKI certificates and Tailnet SOCKS5 proxying."""

from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from infra.terraform.lifecycle.cluster_common import (
    find_repo_root,
    get_cluster_domain,
    get_intranet_domain,
    get_public_domain,
)
from infra.tools.cloud_emulator.access.cluster_tailnet import (
    DEFAULT_GATEWAY_FORWARD_PORT,
    TailnetManager,
    synchronize_cluster_ca,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_CONTROL_CLUSTER = "ctrl-eaws-lh1"
DEFAULT_CELL_CLUSTER = "cell-eaws-lh1"
DEFAULT_SOCKS_ADDRESS = "127.0.0.1:1055"


def find_browser() -> Path:
    candidates = [
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return Path(candidate)
    raise FileNotFoundError("Chromium or Google Chrome executable not found")


def get_spki_pin_openssl(endpoint: str, server_name: str) -> str:
    """Derive SPKI pin from remote TLS endpoint via openssl."""
    cmd = [
        "openssl",
        "s_client",
        "-connect",
        endpoint,
        "-servername",
        server_name,
    ]
    result = subprocess.run(
        cmd,
        input=b"",
        capture_output=True,
        check=False,
    )
    leaf_cert = result.stdout
    if b"BEGIN CERTIFICATE" not in leaf_cert:
        raise ValueError(f"Could not read certificate from {endpoint}")

    # Extract public key in DER format
    pubkey_cmd = ["openssl", "x509", "-pubkey", "-noout"]
    der_cmd = ["openssl", "pkey", "-pubin", "-outform", "DER"]

    p1 = subprocess.Popen(
        pubkey_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    p2 = subprocess.Popen(
        der_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert p1.stdin is not None
    p1.stdin.write(leaf_cert)
    p1.stdin.close()
    der_bytes, _ = p2.communicate()

    return base64.b64encode(hashlib.sha256(der_bytes).digest()).decode("ascii")


def get_default_urls(
    control_cluster: str,
    _cell_cluster: str,
    access_alias_domain: str,
    public_domain: str = "",
) -> list[str]:
    domain = public_domain or f"{control_cluster}.{access_alias_domain}"
    return [
        f"https://home.{domain}",
    ]


def ensure_ca_cert(repo_root: Path, control_cluster: str) -> Path:
    ca_cert, _ = synchronize_cluster_ca(repo_root, control_cluster)
    assert isinstance(ca_cert, Path)
    return ca_cert


def resolve_gateway_ip(control_cluster: str) -> str:
    try:
        ip_cmd = [
            "kubectl",
            "--context",
            control_cluster,
            "get",
            "gateway",
            "private-access",
            "-n",
            "envoy-gateway-system",
            "-o",
            "jsonpath={.status.addresses[0].value}",
        ]
        gateway_ip = subprocess.check_output(ip_cmd, text=True, timeout=5).strip()
        if gateway_ip:
            return gateway_ip
    except Exception:
        pass
    return "172.31.0.11"


def compute_cert_spki_pin(cert_bytes: bytes) -> str:
    pubkey_cmd = ["openssl", "x509", "-pubkey", "-noout"]
    der_cmd = ["openssl", "pkey", "-pubin", "-outform", "DER"]
    p1 = subprocess.Popen(
        pubkey_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    p2 = subprocess.Popen(
        der_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert p1.stdin is not None
    p1.stdin.write(cert_bytes)
    p1.stdin.close()
    der_bytes, _ = p2.communicate()
    return base64.b64encode(hashlib.sha256(der_bytes).digest()).decode("ascii")


def compute_ca_spki_pin(ca_cert: Path) -> str:
    return compute_cert_spki_pin(ca_cert.read_bytes())


def resolve_gateway_spki_pins(clusters: str | Sequence[str]) -> list[str]:
    cluster_list = [clusters] if isinstance(clusters, str) else list(clusters)
    pins: list[str] = []
    for cluster in cluster_list:
        for secret_name in (
            "private-access-tls",
            "private-access-alias-tls",
            "private-access-coder-apps-tls",
        ):
            try:
                crt_b64 = subprocess.check_output(
                    [
                        "kubectl",
                        "--context",
                        cluster,
                        "get",
                        "secret",
                        secret_name,
                        "-n",
                        "envoy-gateway-system",
                        "-o",
                        "jsonpath={.data.tls\\.crt}",
                    ],
                    text=True,
                    timeout=5,
                    stderr=subprocess.DEVNULL,
                ).strip()
                if crt_b64:
                    pin = compute_cert_spki_pin(base64.b64decode(crt_b64))
                    if pin not in pins:
                        pins.append(pin)
            except Exception:
                pass
    return pins


def compute_browser_spki_list(
    ca_cert: Path,
    clusters: str | Sequence[str],
    private_control_domain: str,
    public_domain: str = "",
    upstream_port: int = DEFAULT_GATEWAY_FORWARD_PORT,
) -> str:
    pins = [compute_ca_spki_pin(ca_cert)]
    pins.extend(resolve_gateway_spki_pins(clusters))
    try:
        leaf_pin = get_spki_pin_openssl(
            f"127.0.0.1:{upstream_port}", f"headlamp.{private_control_domain}"
        )
        if leaf_pin not in pins:
            pins.append(leaf_pin)
    except Exception:
        pass
    if public_domain:
        try:
            leaf_pin = get_spki_pin_openssl(
                f"127.0.0.1:{upstream_port}", f"headlamp.{public_domain}"
            )
            if leaf_pin not in pins:
                pins.append(leaf_pin)
        except Exception:
            pass
    return ",".join(pins)


def start_browser_proxy(
    temp_path: Path,
    socks_address: str,
    domains: Sequence[str],
    gateway_ip: str,
    direct_gateway: str | None = None,
) -> tuple[subprocess.Popen[bytes], str]:
    pac_file = temp_path / "proxy.pac"
    ready_file = temp_path / "proxy.ready"
    log_file = temp_path / "proxy.log"

    proxy_script = Path(__file__).with_name("browser_proxy.py")
    pac_file.write_text(
        'function FindProxyForURL(url, host) { return "DIRECT"; }\n', encoding="utf-8"
    )

    map_args = []
    for entry in domains:
        if "=" in entry:
            map_args.extend(["--map", entry])
        else:
            map_args.extend(["--map", f"*.{entry}={gateway_ip}"])

    proxy_cmd = [
        sys.executable,
        str(proxy_script),
        "--listen-address",
        "127.0.0.1:0",
        "--socks-address",
        socks_address,
        *map_args,
        "--pac-file",
        str(pac_file),
        "--ready-file",
        str(ready_file),
    ]
    if direct_gateway:
        proxy_cmd.extend([
            "--direct-gateway",
            direct_gateway,
            "--direct-gateway-target",
            gateway_ip,
        ])
    with log_file.open("a", encoding="utf-8") as log:
        proxy_proc = subprocess.Popen(proxy_cmd, stdout=log, stderr=log)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready_file.is_file() and ready_file.stat().st_size > 0:
            break
        time.sleep(0.05)

    proxy_address = (
        ready_file.read_text(encoding="utf-8").strip()
        if ready_file.is_file()
        else "127.0.0.1:18080"
    )

    pac_domains = [d for d in domains if "=" not in d]
    domain_conditions = " || ".join(
        f'host === "{domain}" || host.endsWith(".{domain}") || dnsDomainIs(host, ".{domain}")'
        for domain in pac_domains
    )
    pac_file.write_text(
        f"""function FindProxyForURL(url, host) {{
  if ({domain_conditions}) {{
    return "PROXY {proxy_address}";
  }}
  return "DIRECT";
}}
""",
        encoding="utf-8",
    )
    return proxy_proc, proxy_address


def _ensure_tailnet(
    repo_root: Path,
    control_cluster: str,
    public_domain: str,
) -> TailnetManager:
    tailnet = TailnetManager(
        repo_root=repo_root,
        control_cluster=control_cluster,
        public_domain=public_domain,
    )
    tailnet.start_gateway_forward()

    if not tailnet.is_daemon_running():
        with contextlib.suppress(Exception):
            tailnet.up()
    return tailnet


def launch_browser(
    requested_urls: Sequence[str] | None = None,
    control_cluster: str = DEFAULT_CONTROL_CLUSTER,
    cell_cluster: str = DEFAULT_CELL_CLUSTER,
    public_domain: str | None = None,
    socks_address: str = DEFAULT_SOCKS_ADDRESS,
) -> None:

    browser = find_browser()
    repo_root = find_repo_root()
    ca_cert = ensure_ca_cert(repo_root, control_cluster)

    if public_domain is None:
        public_domain = get_public_domain(repo_root)
    intranet_domain = get_intranet_domain(repo_root)

    cluster_domain = get_cluster_domain(repo_root)
    access_alias_domain = cluster_domain
    private_control_domain = f"{control_cluster}.{cluster_domain}"
    urls = (
        list(requested_urls)
        if requested_urls
        else get_default_urls(control_cluster, cell_cluster, cluster_domain, intranet_domain)
    )

    tailnet = _ensure_tailnet(repo_root, control_cluster, public_domain)

    temp_dir = tempfile.mkdtemp(prefix="cluster-browser-")
    temp_path = Path(temp_dir)
    gateway_ip = resolve_gateway_ip(control_cluster)
    cell_gateway_ip = resolve_gateway_ip(cell_cluster)
    direct_gateway = f"127.0.0.1:{tailnet.upstream_port}"
    proxy_domains = [
        access_alias_domain,
        f"*.{cell_cluster}.{access_alias_domain}={cell_gateway_ip}",
    ]
    if public_domain:
        proxy_domains.extend([
            public_domain,
            f"*.{cell_cluster}.{public_domain}={cell_gateway_ip}",
        ])
    proxy_proc, proxy_address = start_browser_proxy(
        temp_path,
        socks_address,
        proxy_domains,
        gateway_ip,
        direct_gateway=direct_gateway,
    )

    def cleanup() -> None:
        try:
            proxy_proc.terminate()
            proxy_proc.wait(timeout=2)
        except Exception:
            pass
        shutil.rmtree(temp_path, ignore_errors=True)

    atexit.register(cleanup)
    spki_list = compute_browser_spki_list(
        ca_cert,
        [control_cluster, cell_cluster],
        private_control_domain,
        public_domain,
        upstream_port=tailnet.upstream_port,
    )

    browser_args = [
        str(browser),
        f"--user-data-dir={temp_path / 'profile'}",
        f"--ignore-certificate-errors-spki-list={spki_list}",
        "--test-type",
        f"--proxy-pac-url=http://{proxy_address}/proxy.pac",
        "--no-default-browser-check",
        "--no-first-run",
        "--new-window",
        *urls,
    ]
    try:
        browser_proc = subprocess.Popen(browser_args)
        browser_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        atexit.unregister(cleanup)
        cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open local cluster services in Chromium.")
    parser.add_argument("urls", nargs="*", help="Optional specific HTTPS URLs to open.")
    args = parser.parse_args(argv)

    try:
        launch_browser(args.urls)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
