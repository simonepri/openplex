"""Tests Python S3 resolver client resolution, HTTP fallback, and URL parsing."""

import contextlib
import http.server
import json
import socketserver
import tempfile
import threading
import unittest
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any, override

from src.infra.tools.s3_resolver.client.python.client import (
    DEFAULT_RESOLVER_URL,
    S3ResolverClient,
    resolve_s3_uri,
)

TARGET_URI = "s3://cell-aws-usw2/home/team-a/data.parquet"


class _ExcHolder:
    def __init__(self) -> None:
        self.value: Any = None


@contextlib.contextmanager
def _assert_raises(exc_type: type[BaseException], match: str | None = None) -> Iterator[_ExcHolder]:
    holder = _ExcHolder()
    try:
        yield holder
    except exc_type as exc:
        holder.value = exc
    else:
        msg = f"Expected {exc_type.__name__} to be raised"
        raise AssertionError(msg)
    if match is not None:
        assert match in str(holder.value)


class MockHandler(http.server.BaseHTTPRequestHandler):
    auth: str | None = None
    count = 0

    def do_GET(self) -> None:
        MockHandler.count += 1
        MockHandler.auth = self.headers.get("Authorization")
        if "team-a" in self.path:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({
                    "mode": "direct",
                    "uri": TARGET_URI,
                    "bucket": "prod-cell-aws-usw2-home",
                    "key": "home/team-a/data.parquet",
                    "endpoint_url": "https://s3.us-west-2.amazonaws.com",
                    "region": "us-west-2",
                    "auth": {"type": "ambient_workload_identity"},
                }).encode()
            )
        elif "team-500" in self.path:
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.end_headers()
        else:
            self.send_response(HTTPStatus.FORBIDDEN)
            self.end_headers()

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


class S3ResolverClientTest(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), MockHandler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/resolve"
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_direct_resolution(self) -> None:
        client = S3ResolverClient(resolver_url=self.url)
        target = client.resolve(TARGET_URI)
        assert target.mode == "direct"
        assert target.is_direct
        assert target.bucket == "prod-cell-aws-usw2-home"
        assert target.key == "home/team-a/data.parquet"
        assert target.endpoint_url == "https://s3.us-west-2.amazonaws.com"
        assert target.clean_endpoint == "s3.us-west-2.amazonaws.com"
        assert target.scheme == "https"
        assert target.region == "us-west-2"
        assert target.auth_type == "ambient_workload_identity"

    def test_boto3_kwargs(self) -> None:
        client = S3ResolverClient(resolver_url=self.url)
        target = client.resolve(TARGET_URI)
        kwargs = target.boto3_client_kwargs()
        assert kwargs == {
            "endpoint_url": "https://s3.us-west-2.amazonaws.com",
            "region_name": "us-west-2",
        }

    def test_caching(self) -> None:
        client = S3ResolverClient(resolver_url=self.url)
        initial_count = MockHandler.count
        target1 = client.resolve(TARGET_URI)
        target2 = client.resolve(TARGET_URI)
        assert target1 is target2
        assert MockHandler.count == initial_count + 1

    def test_authorization_header_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("mock-jwt-token")
            client = S3ResolverClient(resolver_url=self.url, token_path=str(token_file))
            client.resolve(TARGET_URI)
            assert MockHandler.auth == "Bearer mock-jwt-token"

    @staticmethod
    def test_fallback() -> None:
        client = S3ResolverClient(resolver_url="http://127.0.0.1:1/resolve", timeout=0.1)
        target = client.resolve(TARGET_URI)
        assert (target.mode, target.bucket, target.auth_type) == (
            "proxy",
            "cell-aws-usw2",
            "ambient_workload_identity",
        )
        assert not target.is_direct
        assert target.endpoint_url == "http://s3-gateway.s3-system.svc:10080"
        assert DEFAULT_RESOLVER_URL == "http://s3-gateway.s3-system.svc:10080/resolve"

    @staticmethod
    def test_invalid_uri() -> None:
        with _assert_raises(ValueError):
            S3ResolverClient().resolve("https://bad/uri")

    def test_convenience_function(self) -> None:
        target = resolve_s3_uri(TARGET_URI, resolver_url=self.url)
        assert target.mode == "direct"

    def test_authorization_error_raises(self) -> None:
        client = S3ResolverClient(resolver_url=self.url, token_path="/nonexistent")
        with _assert_raises(
            PermissionError,
            match="s3 resolver authorization failed with status 403",
        ):
            client.resolve("s3://cell-aws-usw2/home/team-b/data.parquet")

    def test_server_error_raises(self) -> None:
        client = S3ResolverClient(resolver_url=self.url, token_path="/nonexistent")
        with _assert_raises(
            RuntimeError,
            match="s3 resolver request failed with status 500",
        ):
            client.resolve("s3://cell-aws-usw2/home/team-500/data.parquet")


if __name__ == "__main__":
    unittest.main()
