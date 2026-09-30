#!/usr/bin/env python3
"""Tests HTTP routing, header manipulation, and browser connection handling in the Paseo Coder proxy server."""

from __future__ import annotations

import socket
import socketserver
import threading
import unittest

try:
    from src.infra.definitions.workspaces.templates.dev.container.apps.paseo_coder_proxy import (
        CoderProxyServer,
        rewrite_initial_connection_hint,
        rewrite_request_head,
    )
except ImportError:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from paseo_coder_proxy import (
        CoderProxyServer,
        rewrite_initial_connection_hint,
        rewrite_request_head,
    )

EXTERNAL_HOST = b"paseo--dev--examples.coder.ctrl-eaws-lh1.k8s.unit.test"


def read_head(connection: socket.socket) -> bytes:
    received = bytearray()
    while b"\r\n\r\n" not in received:
        received.extend(connection.recv(4096))
    return bytes(received)


class UpstreamServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, handler: type[socketserver.BaseRequestHandler]) -> None:
        self.request_head = b""
        super().__init__(("127.0.0.1", 0), handler)


class HttpHandler(socketserver.BaseRequestHandler):
    server: UpstreamServer
    request: socket.socket

    def handle(self) -> None:
        self.server.request_head = read_head(self.request)
        body = b"Paseo UI"
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 8\r\n\r\n" + body
        )


class WebSocketHandler(socketserver.BaseRequestHandler):
    server: UpstreamServer
    request: socket.socket

    def handle(self) -> None:
        self.server.request_head = read_head(self.request)
        self.request.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
        )
        self.request.sendall(self.request.recv(4))


def web_ui_body(upstream_port: int) -> bytes:
    return (
        b"<html><head>"
        b'<script>window.__PASEO_INITIAL_DAEMON_CONNECTION__={"listen":"localhost:'
        + str(upstream_port).encode()
        + b'","useTls":true,"label":"workspace"}</script>'
        b"</head><body></body></html>"
    )


class WebUiHandler(socketserver.BaseRequestHandler):
    server: UpstreamServer
    request: socket.socket

    def handle(self) -> None:
        self.server.request_head = read_head(self.request)
        body = web_ui_body(self.server.server_address[1])
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b'ETag: "stale"\r\n' + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )


class ChunkedWebUiHandler(socketserver.BaseRequestHandler):
    server: UpstreamServer
    request: socket.socket

    def handle(self) -> None:
        self.server.request_head = read_head(self.request)
        body = web_ui_body(self.server.server_address[1])
        split = len(body) // 2
        chunks = b"".join(
            f"{len(part):x}".encode() + b"\r\n" + part + b"\r\n"
            for part in (body[:split], body[split:])
        )
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n"
        )


class InvalidChunkHandler(socketserver.BaseRequestHandler):
    server: UpstreamServer
    request: socket.socket

    def handle(self) -> None:
        self.server.request_head = read_head(self.request)
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Transfer-Encoding: chunked\r\n\r\nzz\r\nboom\r\n0\r\n\r\n"
        )


class PaseoCoderProxyTest(unittest.TestCase):
    def run_servers(
        self, handler: type[socketserver.BaseRequestHandler]
    ) -> tuple[UpstreamServer, CoderProxyServer, list[threading.Thread]]:
        upstream = UpstreamServer(handler)
        proxy = CoderProxyServer(0, upstream.server_address[1], EXTERNAL_HOST.decode())
        threads = [
            threading.Thread(target=upstream.serve_forever),
            threading.Thread(target=proxy.serve_forever),
        ]
        for thread in threads:
            thread.start()
        return upstream, proxy, threads

    def stop_servers(
        self,
        upstream: UpstreamServer,
        proxy: CoderProxyServer,
        threads: list[threading.Thread],
    ) -> None:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
        for thread in threads:
            thread.join(timeout=2)

    def test_http_reaches_ui_with_loopback_host(self) -> None:
        upstream, proxy, threads = self.run_servers(HttpHandler)
        try:
            with socket.create_connection(
                ("127.0.0.1", proxy.server_address[1]), timeout=2
            ) as client:
                client.sendall(b"GET / HTTP/1.1\r\nHo")
                client.sendall(
                    b"st: "
                    + EXTERNAL_HOST
                    + b"\r\nX-Forwarded-Host: attacker.invalid\r\n"
                    + b"X-Forwarded-Proto: http\r\nAccept-Encoding: gzip\r\n"
                    + b"Connection: keep-alive\r\n\r\n"
                )
                response = bytearray()
                while payload := client.recv(4096):
                    response.extend(payload)
            self.assertIn(b"200 OK", response)
            self.assertTrue(response.endswith(b"Paseo UI"))
            self.assertIn(
                f"Host: localhost:{upstream.server_address[1]}".encode(),
                upstream.request_head,
            )
            self.assertIn(b"X-Forwarded-Host: " + EXTERNAL_HOST, upstream.request_head)
            self.assertIn(b"X-Forwarded-Proto: https", upstream.request_head)
            self.assertNotIn(b"attacker.invalid", upstream.request_head)
            self.assertIn(b"Accept-Encoding: gzip", upstream.request_head)
            self.assertIn(b"Connection: close", upstream.request_head)
        finally:
            self.stop_servers(upstream, proxy, threads)

    def test_duplicate_host_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "one Host header"):
            rewrite_request_head(
                b"GET / HTTP/1.1\r\nHost: first.invalid\r\nHost: second.invalid",
                6767,
            )

    def test_document_bootstraps_the_coder_app_origin(self) -> None:
        upstream, proxy, threads = self.run_servers(WebUiHandler)
        try:
            with socket.create_connection(
                ("127.0.0.1", proxy.server_address[1]), timeout=2
            ) as client:
                client.sendall(
                    b"GET /welcome HTTP/1.1\r\nHost: "
                    + EXTERNAL_HOST
                    + b"\r\nAccept: text/html\r\nIf-None-Match: stale\r\n\r\n"
                )
                response = bytearray()
                while payload := client.recv(4096):
                    response.extend(payload)
            head, body = bytes(response).split(b"\r\n\r\n", 1)
            self.assertIn(b"200 OK", head)
            self.assertNotIn(b"ETag:", head)
            self.assertIn(f"Content-Length: {len(body)}".encode(), head)
            self.assertIn(
                b'window.__PASEO_INITIAL_DAEMON_CONNECTION__={"listen":"'
                + EXTERNAL_HOST
                + b':443","useTls":true,"label":"workspace"}',
                body,
            )
            self.assertNotIn(f'"listen":"localhost:{upstream.server_address[1]}"'.encode(), body)
            self.assertNotIn(b"If-None-Match", upstream.request_head)
        finally:
            self.stop_servers(upstream, proxy, threads)

    def test_chunked_document_is_normalized_and_rewritten(self) -> None:
        upstream, proxy, threads = self.run_servers(ChunkedWebUiHandler)
        try:
            with socket.create_connection(
                ("127.0.0.1", proxy.server_address[1]), timeout=2
            ) as client:
                client.sendall(
                    b"GET /welcome HTTP/1.1\r\nHost: "
                    + EXTERNAL_HOST
                    + b"\r\nAccept: text/html\r\nAccept-Encoding: gzip\r\n\r\n"
                )
                response = bytearray()
                while payload := client.recv(4096):
                    response.extend(payload)
            head, body = bytes(response).split(b"\r\n\r\n", 1)
            self.assertIn(b"200 OK", head)
            self.assertNotIn(b"Transfer-Encoding", head)
            self.assertIn(f"Content-Length: {len(body)}".encode(), head)
            self.assertIn(
                b'window.__PASEO_INITIAL_DAEMON_CONNECTION__={"listen":"'
                + EXTERNAL_HOST
                + b':443","useTls":true,"label":"workspace"}',
                body,
            )
            self.assertNotIn(b"Accept-Encoding", upstream.request_head)
        finally:
            self.stop_servers(upstream, proxy, threads)

    def test_invalid_chunked_document_is_rejected(self) -> None:
        upstream, proxy, threads = self.run_servers(InvalidChunkHandler)
        try:
            with socket.create_connection(
                ("127.0.0.1", proxy.server_address[1]), timeout=2
            ) as client:
                client.sendall(
                    b"GET / HTTP/1.1\r\nHost: " + EXTERNAL_HOST + b"\r\nAccept: text/html\r\n\r\n"
                )
                response = bytearray()
                while payload := client.recv(4096):
                    response.extend(payload)
            self.assertIn(b"502 Bad Gateway", response)
        finally:
            self.stop_servers(upstream, proxy, threads)

    def test_initial_hint_requires_one_valid_script(self) -> None:
        with self.assertRaisesRegex(ValueError, "one initial connection hint"):
            rewrite_initial_connection_hint(b"<html></html>", EXTERNAL_HOST.decode())

    def test_websocket_upgrade_is_tunneled(self) -> None:
        upstream, proxy, threads = self.run_servers(WebSocketHandler)
        try:
            with socket.create_connection(
                ("127.0.0.1", proxy.server_address[1]), timeout=2
            ) as client:
                client.sendall(
                    b"GET /ws HTTP/1.1\r\nHost: "
                    + EXTERNAL_HOST
                    + b"\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
                )
                response = read_head(client)
                self.assertIn(b"101 Switching Protocols", response)
                client.sendall(b"ping")
                self.assertEqual(client.recv(4), b"ping")
            self.assertIn(
                f"Host: localhost:{upstream.server_address[1]}".encode(),
                upstream.request_head,
            )
            self.assertIn(b"Connection: Upgrade", upstream.request_head)
        finally:
            self.stop_servers(upstream, proxy, threads)


if __name__ == "__main__":
    unittest.main()
