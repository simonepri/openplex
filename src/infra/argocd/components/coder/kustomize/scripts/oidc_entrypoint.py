"""Redirect Coder's public root to OIDC with a non-root return path."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

PORT = 8080


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/ready":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/api/v2/users/oidc/callback?redirect=/templates")
        self.end_headers()

    @override
    def log_message(self, format: str, *args: Any) -> None:
        del format, args


ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
