"""Verify generated publication launchers preserve arguments and return usable image references."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar, override
from unittest.mock import patch

from src.bazel.rules.oci import workload_cli


class PublicationLauncherTest(unittest.TestCase):
    publisher: ClassVar[str]
    digest: ClassVar[str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.publisher = str(Path(sys.argv[1]).resolve())
        cls.digest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))["manifests"][0][
            "digest"
        ]

    def test_run_consumes_reference_json_from_direct_publisher_execution(self) -> None:
        requests: list[str] = []
        digest = self.digest

        class RegistryProxy(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Docker-Content-Digest", digest)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), RegistryProxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as workspace:
                environment = {
                    "BUILD_WORKSPACE_DIRECTORY": workspace,
                    "PATH": os.defpath,
                    "WORKLOAD_REGISTRY": "origin-registry:5000/000000000000/us-east-1",
                    "WORKLOAD_REGISTRY_INSECURE": "true",
                    "WORKLOAD_STREAM_TAG": "dev-20260901T120000Z_0123456789ab",
                    "http_proxy": f"http://127.0.0.1:{server.server_port}",
                    "no_proxy": "",
                    "NO_PROXY": "",
                }
                with patch.dict(os.environ, environment):
                    references = workload_cli._publish_and_resolve_images(
                        self.publisher, "cell.registry/fixture"
                    )
            assert references == {"": f"cell.registry/fixture@{digest}"}
            assert requests == [
                "http://origin-registry:5000/v2/000000000000/us-east-1/src/examples/fixture/manifests/dev-20260901T120000Z_0123456789ab"
            ]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
