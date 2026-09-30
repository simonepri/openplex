#!/usr/bin/env python3
"""Proves the local TLS bridge preserves a long-lived bidirectional stream."""

from __future__ import annotations

import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("tls_bridge.py")
HOSTNAME = "headscale.test"


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class EchoServer:
    def __init__(self, certificate: Path, key: Path) -> None:
        self.port = available_port()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self) -> EchoServer:
        self.thread.start()
        if not self.ready.wait(timeout=5):
            raise RuntimeError("TLS test server did not start")
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(timeout=5)

    def serve(self) -> None:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(1)
            self.ready.set()
            connection, _ = listener.accept()
            with self.context.wrap_socket(connection, server_side=True) as stream:
                for _ in range(2):
                    payload = stream.recv(65536)
                    stream.sendall(b"reply:" + payload)


class DelayedReplyServer:
    def __init__(self, certificate: Path, key: Path) -> None:
        self.port = available_port()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self) -> DelayedReplyServer:
        self.thread.start()
        if not self.ready.wait(timeout=5):
            raise RuntimeError("TLS test server did not start")
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(timeout=5)

    def serve(self) -> None:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(1)
            self.ready.set()
            connection, _ = listener.accept()
            with self.context.wrap_socket(connection, server_side=True) as stream:
                payload = stream.recv(65536)
                time.sleep(0.2)
                stream.sendall(b"reply:" + payload)


class DisconnectObserver:
    def __init__(self, certificate: Path, key: Path) -> None:
        self.port = available_port()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.ready = threading.Event()
        self.disconnected = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self) -> DisconnectObserver:
        self.thread.start()
        if not self.ready.wait(timeout=5):
            raise RuntimeError("TLS test server did not start")
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(timeout=5)

    def serve(self) -> None:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(1)
            self.ready.set()
            connection, _ = listener.accept()
            with self.context.wrap_socket(connection, server_side=True) as stream:
                while stream.recv(65536):
                    pass
                self.disconnected.set()


def create_certificate(directory: Path) -> tuple[Path, Path]:
    certificate = directory / "certificate.pem"
    key = directory / "key.pem"
    configuration = directory / "openssl.cnf"
    configuration.write_text(
        "\n".join((
            "[req]",
            "distinguished_name = subject",
            "x509_extensions = extensions",
            "prompt = no",
            "[subject]",
            f"CN = {HOSTNAME}",
            "[extensions]",
            f"subjectAltName = DNS:{HOSTNAME}",
            "basicConstraints = critical,CA:TRUE",
            "keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign",
            "",
        ))
    )
    subprocess.run(
        (
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
        ),
        check=True,
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    return certificate, key


class TLSBridgeTest(unittest.TestCase):
    def _run_relay_test(self, listener_tls: bool) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            certificate, key = create_certificate(directory)
            ready_file = directory / "bridge.ready"
            bridge_port = available_port()
            with EchoServer(certificate, key) as upstream:
                command = [
                    "python3",
                    str(SCRIPT),
                    "--listen-host",
                    "127.0.0.1",
                    "--listen-port",
                    str(bridge_port),
                    "--upstream-address",
                    "127.0.0.1",
                    "--upstream-host",
                    HOSTNAME,
                    "--upstream-port",
                    str(upstream.port),
                    "--ca-file",
                    str(certificate),
                    "--ready-file",
                    str(ready_file),
                ]
                if listener_tls:
                    command.extend((
                        "--listen-cert-file",
                        str(certificate),
                        "--listen-key-file",
                        str(key),
                    ))
                bridge = subprocess.Popen(
                    command,
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )
                try:
                    for _ in range(100):
                        if ready_file.is_file():
                            break
                        if bridge.poll() is not None:
                            self.fail(f"bridge exited early with status {bridge.returncode}")
                        time.sleep(0.05)
                    else:
                        self.fail("bridge did not become ready")

                    client = socket.create_connection(("127.0.0.1", bridge_port))
                    if listener_tls:
                        context = ssl.create_default_context(cafile=str(certificate))
                        client = context.wrap_socket(
                            client,
                            server_hostname=HOSTNAME,
                        )
                    with client:
                        client.sendall(b"first")
                        assert client.recv(65536) == b"reply:first"
                        time.sleep(0.1)
                        client.sendall(b"second")
                        assert client.recv(65536) == b"reply:second"
                finally:
                    bridge.terminate()
                    bridge.wait(timeout=5)

    def test_relays_multiple_messages_over_one_connection(self) -> None:
        for listener_tls in (False, True):
            with self.subTest(listener_tls=listener_tls):
                self._run_relay_test(listener_tls)

    def test_preserves_reply_after_client_half_close(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            certificate, key = create_certificate(directory)
            ready_file = directory / "bridge.ready"
            bridge_port = available_port()
            with DelayedReplyServer(certificate, key) as upstream:
                bridge = subprocess.Popen(
                    (
                        "python3",
                        str(SCRIPT),
                        "--listen-host",
                        "127.0.0.1",
                        "--listen-port",
                        str(bridge_port),
                        "--upstream-address",
                        "127.0.0.1",
                        "--upstream-host",
                        HOSTNAME,
                        "--upstream-port",
                        str(upstream.port),
                        "--ca-file",
                        str(certificate),
                        "--ready-file",
                        str(ready_file),
                    ),
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )
                try:
                    for _ in range(100):
                        if ready_file.is_file():
                            break
                        if bridge.poll() is not None:
                            self.fail(f"bridge exited early with status {bridge.returncode}")
                        time.sleep(0.05)
                    else:
                        self.fail("bridge did not become ready")

                    with socket.create_connection(("127.0.0.1", bridge_port)) as client:
                        client.sendall(b"request")
                        client.shutdown(socket.SHUT_WR)
                        assert client.recv(65536) == b"reply:request"
                finally:
                    bridge.terminate()
                    bridge.wait(timeout=5)

    def test_closes_upstream_after_disconnected_client(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            certificate, key = create_certificate(directory)
            ready_file = directory / "bridge.ready"
            bridge_port = available_port()
            with DisconnectObserver(certificate, key) as upstream:
                bridge = subprocess.Popen(
                    (
                        "python3",
                        str(SCRIPT),
                        "--listen-host",
                        "127.0.0.1",
                        "--listen-port",
                        str(bridge_port),
                        "--upstream-address",
                        "127.0.0.1",
                        "--upstream-host",
                        HOSTNAME,
                        "--upstream-port",
                        str(upstream.port),
                        "--ca-file",
                        str(certificate),
                        "--ready-file",
                        str(ready_file),
                    ),
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )
                try:
                    for _ in range(100):
                        if ready_file.is_file():
                            break
                        if bridge.poll() is not None:
                            self.fail(f"bridge exited early with status {bridge.returncode}")
                        time.sleep(0.05)
                    else:
                        self.fail("bridge did not become ready")

                    with socket.create_connection(("127.0.0.1", bridge_port)) as client:
                        client.sendall(b"request")
                    assert upstream.disconnected.wait(timeout=2), (
                        "bridge left the upstream connection open"
                    )
                finally:
                    bridge.terminate()
                    bridge.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
