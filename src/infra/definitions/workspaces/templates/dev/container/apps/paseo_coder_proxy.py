#!/usr/bin/env python3
"""Proxies HTTP requests between Coder workspace application endpoints and local Paseo services without route collisions."""

from __future__ import annotations

import argparse
import json
import re
import selectors
import socket
import socketserver
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

HEADER_LIMIT = 64 * 1024
HTML_BODY_LIMIT = 1024 * 1024
BUFFER_SIZE = 64 * 1024
LOOPBACK = "127.0.0.1"
INITIAL_CONNECTION_PATTERN = re.compile(
    rb"(<script>window\.__PASEO_INITIAL_DAEMON_CONNECTION__=)(.*?)(</script>)"
)
HTTP_REQUEST_LINE_PARTS = 3
HTTP_PARTS_MIN_COUNT = 2
MAX_PORT = 65535


def is_document_request(request_head: bytes) -> bool:
    """Return whether a request asks for a browser document."""
    lines = request_head.split(b"\r\n")
    if not lines or not lines[0].startswith(b"GET "):
        return False
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"accept":
            return b"text/html" in value.lower()
    return False


def _should_skip_header(normalized_name: bytes, *, is_upgrade: bool, for_document: bool) -> bool:
    if normalized_name in {b"host", b"x-forwarded-host", b"x-forwarded-proto"}:
        return True
    if for_document and normalized_name in {
        b"accept-encoding",
        b"if-modified-since",
        b"if-none-match",
    }:
        return True
    return not is_upgrade and normalized_name in {b"connection", b"proxy-connection"}


def rewrite_request_head(
    request_head: bytes, upstream_port: int, *, for_document: bool = False
) -> bytes:
    """Replace Host while retaining the public authority in forwarded headers.

    Document requests also shed caching validators and Accept-Encoding so the
    response arrives complete and unencoded for hint rewriting.
    """
    lines = request_head.split(b"\r\n")
    if not lines or len(lines[0].split(b" ")) != HTTP_REQUEST_LINE_PARTS:
        raise ValueError("invalid HTTP request line")

    parsed_headers: list[tuple[bytes, bytes, bytes]] = []
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator or not name.strip():
            raise ValueError("invalid HTTP header")
        parsed_headers.append((name, value.lstrip(), line))

    host_values = [value for name, value, _ in parsed_headers if name.strip().lower() == b"host"]
    if len(host_values) != 1 or not host_values[0]:
        raise ValueError("HTTP request must contain one Host header")

    connection_tokens = {
        token.strip().lower()
        for name, value, _ in parsed_headers
        if name.strip().lower() == b"connection"
        for token in value.split(b",")
    }
    has_upgrade = any(
        name.strip().lower() == b"upgrade" and value for name, value, _ in parsed_headers
    )
    is_upgrade = b"upgrade" in connection_tokens and has_upgrade
    rewritten = [lines[0], f"Host: localhost:{upstream_port}".encode()]
    for name, _, original in parsed_headers:
        if not _should_skip_header(
            name.strip().lower(), is_upgrade=is_upgrade, for_document=for_document
        ):
            rewritten.append(original)
    rewritten.extend((b"X-Forwarded-Host: " + host_values[0], b"X-Forwarded-Proto: https"))
    if not is_upgrade:
        rewritten.append(b"Connection: close")
    return b"\r\n".join(rewritten) + b"\r\n\r\n"


def rewrite_initial_connection_hint(body: bytes, external_hostname: str) -> bytes:
    """Point Paseo's native browser bootstrap hint back at the Coder app."""
    matches = list(INITIAL_CONNECTION_PATTERN.finditer(body))
    if len(matches) != 1:
        raise ValueError("Paseo HTML must contain one initial connection hint")
    match = matches[0]
    try:
        hint = json.loads(match.group(2))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Paseo HTML contains an invalid initial connection hint") from error
    if not isinstance(hint, dict) or not isinstance(hint.get("listen"), str):
        raise TypeError("Paseo HTML contains an invalid initial connection hint")
    hint["listen"] = f"{external_hostname}:443"
    hint["useTls"] = True
    encoded_hint = (
        json
        .dumps(hint, separators=(",", ":"))
        .replace("<", "\\u003C")
        .replace(">", "\\u003E")
        .replace("&", "\\u0026")
        .encode()
    )
    return body[: match.start(2)] + encoded_hint + body[match.end(2) :]


def _parse_response_headers(header_lines: list[bytes]) -> tuple[bytes, bytes, list[bytes]]:
    content_type = b""
    content_encoding = b""
    rewritten_headers = []
    for line in header_lines:
        name, separator, value = line.partition(b":")
        if not separator or not name.strip():
            raise ValueError("invalid HTTP response header")
        normalized_name = name.strip().lower()
        if normalized_name == b"content-type":
            content_type = value.strip().lower()
        elif normalized_name == b"content-encoding":
            content_encoding = value.strip().lower()
        if normalized_name not in {b"content-length", b"etag"}:
            rewritten_headers.append(line)
    return content_type, content_encoding, rewritten_headers


def rewrite_html_response(
    response_head: bytes, body: bytes, external_hostname: str
) -> tuple[bytes, bytes]:
    """Rewrite a successful Paseo HTML response and its entity headers."""
    lines = response_head.split(b"\r\n")
    if not lines or not lines[0].startswith(b"HTTP/"):
        raise ValueError("invalid HTTP response status line")
    parts = lines[0].split(b" ", 2)
    if len(parts) < HTTP_PARTS_MIN_COUNT or parts[1] != b"200":
        return response_head, body

    content_type, content_encoding, rewritten_headers = _parse_response_headers(lines[1:])
    rewritten_headers.insert(0, lines[0])

    if not content_type.startswith(b"text/html"):
        return response_head, body
    if content_encoding:
        raise ValueError("Paseo HTML response must not be encoded")

    rewritten_body = rewrite_initial_connection_hint(body, external_hostname)
    rewritten_headers.append(f"Content-Length: {len(rewritten_body)}".encode())
    return b"\r\n".join(rewritten_headers), rewritten_body


def read_request_head(client: socket.socket) -> tuple[bytes, bytes]:
    received = bytearray()
    while b"\r\n\r\n" not in received:
        chunk = client.recv(BUFFER_SIZE)
        if not chunk:
            raise ConnectionError("client closed before sending HTTP headers")
        received.extend(chunk)
        if len(received) > HEADER_LIMIT:
            raise OverflowError("HTTP request headers exceed the proxy limit")
    request_head, body = bytes(received).split(b"\r\n\r\n", 1)
    return request_head, body


def _parse_response_framing(header_lines: list[bytes]) -> tuple[bool, list[bytes]]:
    content_lengths = []
    chunked = False
    for line in header_lines:
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        normalized_name = name.strip().lower()
        if normalized_name == b"content-length":
            content_lengths.append(value.strip())
        elif normalized_name == b"transfer-encoding":
            if chunked or value.strip().lower() != b"chunked":
                raise ValueError("Paseo response used an unsupported transfer encoding")
            chunked = True
    return chunked, content_lengths


def _read_identity_body(
    upstream: socket.socket, initial_body: bytes, content_lengths: list[bytes]
) -> bytes:
    if len(content_lengths) != 1 or not content_lengths[0].isdigit():
        raise ValueError("Paseo response must be chunked or carry one Content-Length")
    content_length = int(content_lengths[0])
    if content_length > HTML_BODY_LIMIT:
        raise OverflowError("Paseo HTML response exceeds the proxy limit")
    if len(initial_body) > content_length:
        raise ValueError("Paseo HTML response exceeded its Content-Length")
    body = initial_body
    while len(body) < content_length:
        payload = upstream.recv(min(BUFFER_SIZE, content_length - len(body)))
        if not payload:
            raise ConnectionError("upstream closed before sending the complete response")
        body += payload
    return body


def read_response(upstream: socket.socket) -> tuple[bytes, bytes]:
    """Read one bounded response, normalizing chunked bodies to identity framing."""
    response_head, body = read_request_head(upstream)
    header_lines = response_head.split(b"\r\n")
    chunked, content_lengths = _parse_response_framing(header_lines[1:])
    if chunked:
        if content_lengths:
            raise ValueError("Paseo response mixed Content-Length with chunked framing")
        decoded_body = read_chunked_body(upstream, body)
        retained = [
            line
            for line in header_lines
            if line.partition(b":")[0].strip().lower() != b"transfer-encoding"
        ]
        retained.append(f"Content-Length: {len(decoded_body)}".encode())
        return b"\r\n".join(retained), decoded_body

    full_body = _read_identity_body(upstream, body, content_lengths)
    return response_head, full_body


def _parse_chunk_size(received: bytearray, receive_more: Callable[[], None]) -> tuple[int, int]:
    while (size_end := received.find(b"\r\n")) < 0:
        if len(received) > HEADER_LIMIT:
            raise ValueError("Paseo response chunk size line exceeds the proxy limit")
        receive_more()
    size_field = bytes(received[:size_end]).split(b";", 1)[0].strip()
    if not re.fullmatch(rb"[0-9a-fA-F]+", size_field):
        raise ValueError("Paseo response contained an invalid chunk size")
    return size_end, int(size_field, 16)


def read_chunked_body(upstream: socket.socket, buffered: bytes) -> bytes:
    """Decode a chunked body, enforcing the HTML body limit.

    Trailers are not read: the upstream connection is closed after a document
    response, so nothing consumes bytes past the terminating zero-size chunk.
    """
    received = bytearray(buffered)
    body = bytearray()

    def receive_more() -> None:
        payload = upstream.recv(BUFFER_SIZE)
        if not payload:
            raise ConnectionError("upstream closed before completing a chunked response")
        received.extend(payload)

    while True:
        size_end, chunk_size = _parse_chunk_size(received, receive_more)
        if chunk_size == 0:
            return bytes(body)
        if len(body) + chunk_size > HTML_BODY_LIMIT:
            raise OverflowError("Paseo HTML response exceeds the proxy limit")
        chunk_end = size_end + 2 + chunk_size
        while len(received) < chunk_end + 2:
            receive_more()
        if received[chunk_end : chunk_end + 2] != b"\r\n":
            raise ValueError("Paseo response contained an unterminated chunk")
        body.extend(received[size_end + 2 : chunk_end])
        del received[: chunk_end + 2]


def _relay_event(key: selectors.SelectorKey) -> bool:
    source = key.fileobj
    destination = key.data
    if not isinstance(source, socket.socket) or not isinstance(destination, socket.socket):
        raise TypeError("proxy selector contained a non-socket")
    payload = source.recv(BUFFER_SIZE)
    if not payload:
        return False
    destination.sendall(payload)
    return True


def _relay_loop(selector: selectors.BaseSelector) -> None:
    active = True
    while active:
        for key, _ in selector.select():
            if not _relay_event(key):
                active = False
                break


def relay(client: socket.socket, upstream: socket.socket) -> None:
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, client)
        _relay_loop(selector)


def send_error(client: socket.socket, status: int, reason: str) -> None:
    body = f"{status} {reason}\n".encode()
    try:
        client.sendall(
            f"HTTP/1.1 {status} {reason}\r\n".encode()
            + b"Connection: close\r\n"
            + b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
    except OSError:
        return


class CoderProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen_port: int, upstream_port: int, external_hostname: str) -> None:
        self.upstream_port = upstream_port
        self.external_hostname = external_hostname
        super().__init__((LOOPBACK, listen_port), CoderProxyHandler)


class CoderProxyHandler(socketserver.BaseRequestHandler):
    server: CoderProxyServer
    request: socket.socket

    def _read_and_rewrite_head(self) -> tuple[bytes, bytes, bool] | None:
        try:
            request_head, body = read_request_head(self.request)
            doc_req = is_document_request(request_head)
            rewritten_head = rewrite_request_head(
                request_head,
                self.server.upstream_port,
                for_document=doc_req,
            )
        except OverflowError:
            send_error(self.request, 431, "Request Header Fields Too Large")
        except (ConnectionError, OSError, ValueError):
            send_error(self.request, 400, "Bad Request")
        else:
            return rewritten_head, body, doc_req
        return None

    def _handle_document_response(self, upstream: socket.socket) -> None:
        try:
            upstream.settimeout(10)
            response_head, response_body = read_response(upstream)
            response_head, response_body = rewrite_html_response(
                response_head,
                response_body,
                self.server.external_hostname,
            )
            self.request.sendall(response_head + b"\r\n\r\n" + response_body)
        except (ConnectionError, OSError, OverflowError, ValueError):
            send_error(self.request, 502, "Bad Gateway")

    def _forward_request(
        self, upstream: socket.socket, rewritten_head: bytes, body: bytes, *, document_request: bool
    ) -> None:
        try:
            upstream.sendall(rewritten_head + body)
        except OSError:
            send_error(self.request, 502, "Bad Gateway")
            return
        if document_request:
            self._handle_document_response(upstream)
            return
        self.request.settimeout(None)
        upstream.settimeout(None)
        try:
            relay(self.request, upstream)
        except OSError:
            return

    @override
    def handle(self) -> None:
        self.request.settimeout(10)
        parsed = self._read_and_rewrite_head()
        if parsed is None:
            return
        rewritten_head, body, document_request = parsed

        try:
            upstream = socket.create_connection((LOOPBACK, self.server.upstream_port), timeout=5)
        except OSError:
            send_error(self.request, 502, "Bad Gateway")
            return
        with upstream:
            self._forward_request(upstream, rewritten_head, body, document_request=document_request)


def port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_PORT:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def hostname(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise argparse.ArgumentTypeError("hostname must be lowercase DNS name")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external-hostname", required=True, type=hostname)
    parser.add_argument("--listen-port", required=True, type=port)
    parser.add_argument("--upstream-port", required=True, type=port)
    args = parser.parse_args(argv)
    if args.listen_port == args.upstream_port:
        parser.error("listen and upstream ports must differ")
    with CoderProxyServer(args.listen_port, args.upstream_port, args.external_hostname) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
