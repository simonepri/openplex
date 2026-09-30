#!/usr/bin/env python3
"""Serves the browser PAC and forwards approved HTTPS through isolated SOCKS5."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import select
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from socketserver import BaseRequestHandler, ThreadingTCPServer
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence


class ProxyError(Exception):
    """An invalid request or unavailable upstream tunnel."""


def parse_address(raw_address: str) -> tuple[str, int]:
    host, separator, raw_port = raw_address.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError(f"invalid address: {raw_address}")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid address: {raw_address}") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"invalid address: {raw_address}")
    return host, port


def parse_mapping(raw_mapping: str) -> tuple[str, ipaddress.IPv4Address]:
    host, separator, raw_address = raw_mapping.partition("=")
    if not separator or not host:
        raise argparse.ArgumentTypeError(f"invalid host mapping: {raw_mapping}")
    try:
        address = ipaddress.IPv4Address(raw_address)
    except ipaddress.AddressValueError as error:
        raise argparse.ArgumentTypeError(f"invalid host mapping: {raw_mapping}") from error
    return host.lower(), address


def receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ProxyError("SOCKS5 server closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)


def consume_socks_address(connection: socket.socket, address_type: int) -> None:
    if address_type == 1:
        receive_exact(connection, 4)
    elif address_type == 3:
        receive_exact(connection, receive_exact(connection, 1)[0])
    elif address_type == 4:
        receive_exact(connection, 16)
    else:
        raise ProxyError("SOCKS5 server returned an invalid address type")
    receive_exact(connection, 2)


def _attempt_socks_connect(
    socks_address: tuple[str, int],
    target_address: ipaddress.IPv4Address,
    target_port: int,
) -> socket.socket:
    connection = socket.create_connection(socks_address, timeout=2)
    try:
        connection.sendall(b"\x05\x01\x00")
        if receive_exact(connection, 2) != b"\x05\x00":
            raise ProxyError("SOCKS5 server rejected unauthenticated access")
        connection.sendall(
            b"\x05\x01\x00\x01" + target_address.packed + struct.pack("!H", target_port)
        )
        version, result, _, address_type = receive_exact(connection, 4)
        if version != 5:
            raise ProxyError("SOCKS5 server returned an invalid response")
        consume_socks_address(connection, address_type)
        if result != 0:
            raise ProxyError(f"SOCKS5 connection failed with result {result}")
        connection.settimeout(None)
        return connection
    except Exception:
        connection.close()
        raise


def connect_through_socks(
    socks_address: tuple[str, int],
    target_address: ipaddress.IPv4Address,
    target_port: int,
    deadline: float,
) -> socket.socket:
    last_error: OSError | ProxyError | None = None
    while time.monotonic() < deadline:
        try:
            return _attempt_socks_connect(socks_address, target_address, target_port)
        except (OSError, ProxyError) as error:
            last_error = error
            time.sleep(0.2)
    raise ProxyError(f"VPN tunnel remained unavailable: {last_error}")


def relay(left: socket.socket, right: socket.socket) -> None:
    while True:
        try:
            readable, _, failed = select.select([left, right], [], [left, right], 60)
            if failed:
                return
            for source in readable:
                payload = source.recv(65536)
                if not payload:
                    return
                destination = right if source is left else left
                destination.sendall(payload)
        except OSError:
            return


@dataclass(frozen=True)
class DirectGateway:
    address: tuple[str, int]
    target: ipaddress.IPv4Address | None = None


class BrowserProxy(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        listen_address: tuple[str, int],
        socks_address: tuple[str, int],
        mappings: dict[str, ipaddress.IPv4Address],
        pac_file: Path,
        direct_gateway: DirectGateway | None = None,
    ) -> None:
        self.socks_address = socks_address
        self.mappings = mappings
        self.pac_file = pac_file
        self.direct_gateway = direct_gateway
        super().__init__(listen_address, BrowserProxyHandler)

    def target_address(self, host: str) -> ipaddress.IPv4Address:
        normalized_host = host.lower()
        if address := self.mappings.get(normalized_host):
            return address
        for mapped_host, mapped_address in sorted(
            self.mappings.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if mapped_host.startswith("*.") and normalized_host.endswith(mapped_host[1:]):
                return mapped_address
        raise ProxyError(f"host is not approved: {host}")


class BrowserProxyHandler(BaseRequestHandler):
    server: BrowserProxy

    def handle(self) -> None:
        upstream: socket.socket | None = None
        try:
            method, target, protocol = self.request_line()
            if method == "GET":
                self.serve_pac(target, protocol)
                return
            if method != "CONNECT":
                raise ProxyError("only PAC and HTTPS CONNECT requests are supported")
            authority = target
            host, port = self.parse_authority(authority)
            target_address = self.server.target_address(host)
            can_direct_fallback = self.server.direct_gateway is not None and (
                self.server.direct_gateway.target is None
                or target_address == self.server.direct_gateway.target
            )
            socks_timeout = 2.0 if can_direct_fallback else 20.0
            try:
                upstream = connect_through_socks(
                    self.server.socks_address,
                    target_address,
                    port,
                    time.monotonic() + socks_timeout,
                )
            except (OSError, ProxyError):
                if can_direct_fallback and self.server.direct_gateway:
                    upstream = socket.create_connection(
                        self.server.direct_gateway.address,
                        timeout=5,
                    )
                else:
                    raise
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            relay(self.request, upstream)
        except (OSError, ProxyError):
            with contextlib.suppress(OSError):
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        finally:
            if upstream is not None:
                upstream.close()

    def request_line(self) -> tuple[str, str, str]:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = self.request.recv(4096)
            if not chunk:
                raise ProxyError("browser closed the request")
            request.extend(chunk)
            if len(request) > 16384:
                raise ProxyError("browser request headers are too large")
        request_line = bytes(request).split(b"\r\n", 1)[0]
        try:
            method, target, protocol = request_line.decode("ascii").split(" ")
        except (UnicodeDecodeError, ValueError) as error:
            raise ProxyError("browser sent an invalid request line") from error
        if protocol not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ProxyError("browser sent an unsupported HTTP version")
        return method, target, protocol

    def serve_pac(self, target: str, protocol: str) -> None:
        if target != "/proxy.pac":
            raise ProxyError("only the generated PAC endpoint is served")
        try:
            payload = self.server.pac_file.read_bytes()
        except OSError as error:
            raise ProxyError("generated PAC is unavailable") from error
        self.request.sendall(
            (
                f"{protocol} 200 OK\r\n"
                "Content-Type: application/x-ns-proxy-autoconfig\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + payload
        )

    @staticmethod
    def parse_authority(authority: str) -> tuple[str, int]:
        host, separator, raw_port = authority.rpartition(":")
        if not separator or not host:
            raise ProxyError("CONNECT authority must include a host and port")
        try:
            port = int(raw_port)
        except ValueError as error:
            raise ProxyError("CONNECT authority contains an invalid port") from error
        if port != 443:
            raise ProxyError("only HTTPS port 443 is supported")
        return host, port


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-address", required=True, type=parse_address)
    parser.add_argument("--socks-address", required=True, type=parse_address)
    parser.add_argument("--direct-gateway", type=parse_address)
    parser.add_argument("--direct-gateway-target", type=ipaddress.IPv4Address)
    parser.add_argument("--map", action="append", default=[], type=parse_mapping)
    parser.add_argument("--pac-file", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    options = arguments(argv)
    mappings = dict(options.map)
    if not mappings:
        raise SystemExit("at least one --map is required")
    listen_address = cast("tuple[str, int]", options.listen_address)
    if not ipaddress.ip_address(listen_address[0]).is_loopback:
        raise SystemExit("browser proxy must listen on a loopback address")
    direct_gateway = (
        DirectGateway(options.direct_gateway, options.direct_gateway_target)
        if options.direct_gateway
        else None
    )
    with BrowserProxy(
        listen_address,
        options.socks_address,
        mappings,
        options.pac_file,
        direct_gateway=direct_gateway,
    ) as server:
        bound_host = str(server.server_address[0])
        bound_port = int(server.server_address[1])
        options.ready_file.write_text(f"{bound_host}:{bound_port}\n", encoding="utf-8")
        server.serve_forever()


if __name__ == "__main__":
    main()
