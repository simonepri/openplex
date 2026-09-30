"""Resolves virtual logical S3 URIs into physical AWS S3 and GCS storage coordinates."""

from __future__ import annotations

import importlib
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

DEFAULT_RESOLVER_URL = "http://s3-gateway.s3-system.svc:10080/resolve"
DEFAULT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


@dataclass(frozen=True)
class ResolvedS3Target:
    """Resolved physical coordinates for an S3 storage path."""

    mode: str
    uri: str
    bucket: str
    key: str
    endpoint_url: str
    region: str
    auth_type: str = "ambient_workload_identity"

    @property
    def is_direct(self) -> bool:
        return self.mode in {"direct", "direct_federated"}

    @property
    def clean_endpoint(self) -> str:
        return self.endpoint_url.split("://")[-1]

    @property
    def scheme(self) -> str:
        return "https" if self.endpoint_url.startswith("https://") else "http"

    def pyarrow_filesystem(self) -> object:
        """Construct native pyarrow.fs.S3FileSystem."""
        pafs = importlib.import_module("pyarrow.fs")
        return pafs.S3FileSystem(
            endpoint_override=self.clean_endpoint,
            scheme=self.scheme,
            region=self.region,
        )

    def boto3_client_kwargs(self) -> dict[str, str]:
        return {"endpoint_url": self.endpoint_url, "region_name": self.region}


class S3ResolverClient:
    """Client for querying the cluster S3 URI resolver."""

    def __init__(
        self,
        resolver_url: str | None = None,
        token_path: str | Path | None = None,
        timeout: float = 2.0,
    ) -> None:
        self.resolver_url = resolver_url or os.environ.get("S3_RESOLVER_URL", DEFAULT_RESOLVER_URL)
        self.token_path = Path(
            token_path or os.environ.get("S3_RESOLVER_TOKEN_PATH", DEFAULT_TOKEN_PATH)
        )
        self.timeout = timeout
        self._cache: dict[str, ResolvedS3Target] = {}
        self._lock = threading.Lock()

    def _read_token(self) -> str | None:
        try:
            return self.token_path.read_text().strip()
        except OSError:
            return None

    def resolve(self, uri: str) -> ResolvedS3Target:
        """Resolve a virtual S3 URI to physical coordinates."""
        if not uri.startswith("s3://"):
            raise ValueError(f"URI must start with s3://: {uri}")

        with self._lock:
            if uri in self._cache:
                return self._cache[uri]

        req = urllib.request.Request(f"{self.resolver_url}?uri={urllib.parse.quote(uri, safe='')}")
        if token := self._read_token():
            req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise PermissionError(
                    f"s3 resolver authorization failed with status {e.code}: unauthorized access for uri {uri!r}"
                ) from e
            raise RuntimeError(
                f"s3 resolver request failed with status {e.code}: {e.reason} for uri {uri!r}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError):
            return self._fallback(uri)

        if status != HTTPStatus.OK:
            raise RuntimeError(f"s3 resolver request failed with status {status} for uri {uri!r}")

        data = json.loads(body.decode("utf-8"))
        target = ResolvedS3Target(
            mode=data.get("mode", "direct"),
            uri=data.get("uri", uri),
            bucket=data.get("bucket", ""),
            key=data.get("key", ""),
            endpoint_url=data.get("endpoint_url", ""),
            region=data.get("region", "us-east-1"),
            auth_type=data.get("auth", {}).get("type", "ambient_workload_identity"),
        )
        with self._lock:
            self._cache[uri] = target
        return target

    @staticmethod
    def _fallback(uri: str) -> ResolvedS3Target:
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        endpoint = (
            os.environ.get("AWS_ENDPOINT_URL_S3")
            or os.environ.get("AWS_ENDPOINT_URL")
            or "http://s3-gateway.s3-system.svc:10080"
        )
        return ResolvedS3Target(
            mode="proxy",
            uri=uri,
            bucket=bucket,
            key=key,
            endpoint_url=endpoint,
            region=os.environ.get("AWS_REGION", "us-east-1"),
            auth_type="ambient_workload_identity",
        )


_STATE: dict[str, S3ResolverClient | None] = {"client": None}
_LOCK = threading.Lock()


def resolve_s3_uri(uri: str, resolver_url: str | None = None) -> ResolvedS3Target:
    """Convenience function to resolve a virtual S3 URI."""
    if resolver_url:
        return S3ResolverClient(resolver_url=resolver_url).resolve(uri)
    with _LOCK:
        if _STATE["client"] is None:
            _STATE["client"] = S3ResolverClient()
        client = _STATE["client"]
    return client.resolve(uri)
