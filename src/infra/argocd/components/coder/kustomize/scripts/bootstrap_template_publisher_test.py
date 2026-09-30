"""Prove local Coder bootstrap first-run, repeat-run, and deny behavior."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import bootstrap_template_publisher

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_EMAIL = "operator@example.invalid"
FIXTURE_PASSWORD = "public-local-fixture"
OWNER_SESSION = "owner-session-value"
EXPECTED_TOKEN_FILE_MODE = 0o440
EXPECTED_REPEAT_LOGOUT_CALLS = 2


class ServerState:
    def __init__(self) -> None:
        self.coder_url = ""
        self.dex_url = ""
        self.owner_exists = False
        self.non_owner = False
        self.redirect_attack = ""
        self.logout_status = 200
        self.login_posts = 0
        self.owner_created = 0
        self.logout_calls = 0
        self.token_requests: list[dict[str, object]] = []
        self.deleted_token_ids: list[str] = []
        self.current_token_id = ""


class QuietHandler(BaseHTTPRequestHandler):
    state: ServerState

    @override
    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def json_response(self, status: int, value: object) -> None:
        encoded = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def empty_response(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def request_json(self) -> object:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length))

    def has_owner_session(self) -> bool:
        return self.headers.get("Coder-Session-Token") == OWNER_SESSION


class CoderHandler(QuietHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/v2/users/oidc/callback":
            self.oidc_callback(parsed.query)
        elif parsed.path == "/":
            self.empty_response(200)
        elif parsed.path == "/api/v2/users/me":
            self.current_user()
        elif parsed.path == "/api/v2/users/me/keys/tokens/local-template-reconciler":
            self.publisher_token()
        else:
            self.empty_response(404)

    def oidc_callback(self, query: str) -> None:
        if "code" not in urllib.parse.parse_qs(query):
            target = self.state.redirect_attack or f"{self.state.dex_url}/auth/local"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if not self.state.owner_exists:
            self.state.owner_exists = True
            self.state.owner_created += 1
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"coder_session_token={OWNER_SESSION}; Path=/; HttpOnly")
        self.end_headers()

    def current_user(self) -> None:
        if not self.has_owner_session():
            self.empty_response(401)
            return
        roles = [{"name": "member"}]
        if not self.state.non_owner:
            roles.append({"name": "owner"})
        self.json_response(
            200,
            {
                "email": FIXTURE_EMAIL,
                "login_type": "oidc",
                "roles": roles,
            },
        )

    def publisher_token(self) -> None:
        if not self.has_owner_session():
            self.empty_response(401)
        elif not self.state.current_token_id:
            self.empty_response(404)
        else:
            self.json_response(200, {"id": self.state.current_token_id})

    def do_DELETE(self) -> None:
        if not self.has_owner_session():
            self.empty_response(401)
            return
        prefix = "/api/v2/users/me/keys/"
        if not self.path.startswith(prefix):
            self.empty_response(404)
            return
        token_id = urllib.parse.unquote(self.path.removeprefix(prefix))
        if token_id != self.state.current_token_id:
            self.empty_response(404)
            return
        self.state.deleted_token_ids.append(token_id)
        self.state.current_token_id = ""
        self.empty_response(204)

    def do_POST(self) -> None:
        if not self.has_owner_session():
            self.empty_response(401)
            return
        if self.path == "/api/v2/users/me/keys/tokens":
            value = self.request_json()
            assert isinstance(value, dict)
            self.state.token_requests.append(value)
            sequence = len(self.state.token_requests)
            self.state.current_token_id = f"publisher-token-{sequence}"
            self.json_response(201, {"key": f"scoped-token-value-{sequence}"})
            return
        if self.path == "/api/v2/users/logout":
            self.state.logout_calls += 1
            if self.state.logout_status != HTTPStatus.OK:
                self.empty_response(self.state.logout_status)
                return
            self.json_response(HTTPStatus.OK, {"message": "Logged out"})
            return
        self.empty_response(404)


class DexHandler(QuietHandler):
    def do_GET(self) -> None:
        if not self.path.startswith("/auth/local"):
            self.empty_response(404)
            return
        encoded = b'<form method="post" action="/auth/local?back=&amp;state=test"><input name="req" value="fixture"></form>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        if not self.path.startswith("/auth/local"):
            self.empty_response(404)
            return
        self.state.login_posts += 1
        length = int(self.headers.get("Content-Length", "0"))
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if fields.get("login") != [FIXTURE_EMAIL] or fields.get("password") != [FIXTURE_PASSWORD]:
            self.empty_response(401)
            return
        self.send_response(302)
        self.send_header(
            "Location", f"{self.state.coder_url}/api/v2/users/oidc/callback?code=valid"
        )
        self.end_headers()


@contextmanager
def fake_servers(state: ServerState) -> Iterator[None]:
    coder = ThreadingHTTPServer(("127.0.0.1", 0), CoderHandler)
    dex = ThreadingHTTPServer(("127.0.0.1", 0), DexHandler)
    CoderHandler.state = state
    DexHandler.state = state
    state.coder_url = f"http://127.0.0.1:{coder.server_port}"
    state.dex_url = f"http://127.0.0.1:{dex.server_port}"
    threads = [
        threading.Thread(target=coder.serve_forever),
        threading.Thread(target=dex.serve_forever),
    ]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        coder.shutdown()
        dex.shutdown()
        coder.server_close()
        dex.server_close()
        for thread in threads:
            thread.join()


class BootstrapTemplatePublisherTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.subject = Path(bootstrap_template_publisher.__file__)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.token_file = Path(self.temp_dir.name) / "coder-session-token"

    def run_subject(
        self,
        state: ServerState,
        *,
        allow_insecure: bool = True,
        password: str = FIXTURE_PASSWORD,
        callback_url: str = "",
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update({
            "CODER_BOOTSTRAP_OIDC_ISSUER": state.dex_url,
            "CODER_BOOTSTRAP_OIDC_PASSWORD": password,
            "CODER_BOOTSTRAP_OIDC_USERNAME": FIXTURE_EMAIL,
            "CODER_BOOTSTRAP_URL": state.coder_url,
            "CODER_SESSION_TOKEN_FILE": str(self.token_file),
            "CODER_URL": state.coder_url,
        })
        if callback_url:
            environment["CODER_SNAPSHOT_CALLBACK_URL"] = callback_url
        else:
            environment.pop("CODER_SNAPSHOT_CALLBACK_URL", None)
        if allow_insecure:
            environment["CODER_BOOTSTRAP_ALLOW_INSECURE"] = "true"
        else:
            environment.pop("CODER_BOOTSTRAP_ALLOW_INSECURE", None)
        return subprocess.run(
            [sys.executable, self.subject],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=10,
        )

    @staticmethod
    def assert_credentials_absent(completed: subprocess.CompletedProcess[str]) -> None:
        output = completed.stdout + completed.stderr
        for credential in [FIXTURE_PASSWORD, OWNER_SESSION, "scoped-token-value"]:
            assert credential not in output

    def test_first_and_repeat_runs_replace_the_ephemeral_scoped_token(self) -> None:
        state = ServerState()
        with fake_servers(state):
            first = self.run_subject(state)
            assert first.returncode == 0, first.stderr
            assert self.token_file.read_text() == "scoped-token-value-1"
            assert stat.S_IMODE(self.token_file.stat().st_mode) == EXPECTED_TOKEN_FILE_MODE
            assert state.owner_created == 1
            assert state.logout_calls == 1
            assert state.deleted_token_ids == []
            self.assert_credentials_absent(first)

            second = self.run_subject(state)
            assert second.returncode == 0, second.stderr
            assert self.token_file.read_text() == "scoped-token-value-2"
            assert stat.S_IMODE(self.token_file.stat().st_mode) == EXPECTED_TOKEN_FILE_MODE
            assert state.owner_created == 1
            assert state.logout_calls == EXPECTED_REPEAT_LOGOUT_CALLS
            assert state.deleted_token_ids == ["publisher-token-1"]
            self.assert_credentials_absent(second)

        expected_request = {
            "lifetime": 3_600_000_000_000,
            "scopes": [
                "coder:templates.author",
                "coder:templates.build",
                "organization:read",
                "user:read",
            ],
            "token_name": "local-template-reconciler",
        }
        assert state.token_requests == [expected_request, expected_request]

    def test_invalid_oidc_credentials_fail_before_token_creation(self) -> None:
        state = ServerState()
        with fake_servers(state):
            completed = self.run_subject(state, password="rejected-fixture-password")
        assert completed.returncode != 0
        assert not self.token_file.exists()
        assert state.token_requests == []
        assert state.logout_calls == 0
        self.assert_credentials_absent(completed)
        assert "rejected-fixture-password" not in completed.stderr

    def test_non_owner_oidc_identity_fails_closed_and_logs_out(self) -> None:
        state = ServerState()
        state.owner_exists = True
        state.non_owner = True
        with fake_servers(state):
            completed = self.run_subject(state)
        assert completed.returncode != 0
        assert not self.token_file.exists()
        assert state.token_requests == []
        assert state.logout_calls == 1
        self.assert_credentials_absent(completed)

    def test_untrusted_redirect_fails_before_sending_oidc_credentials(self) -> None:
        state = ServerState()
        state.redirect_attack = "http://127.0.0.1:9/collect"
        with fake_servers(state):
            completed = self.run_subject(state)
        assert completed.returncode != 0
        assert not self.token_file.exists()
        assert state.login_posts == 0
        assert state.token_requests == []
        self.assert_credentials_absent(completed)

    def test_insecure_browser_endpoints_are_test_only(self) -> None:
        state = ServerState()
        with fake_servers(state):
            completed = self.run_subject(state, allow_insecure=False)
        assert completed.returncode != 0
        assert not self.token_file.exists()
        assert state.login_posts == 0
        assert state.token_requests == []
        self.assert_credentials_absent(completed)

    def test_registration_failure_is_sanitized_and_logs_out_before_token_handoff(self) -> None:
        state = ServerState()
        with fake_servers(state):
            completed = self.run_subject(
                state, callback_url="http://invalid.example/oauth/callback"
            )
        assert completed.returncode != 0
        assert state.logout_calls == 1
        assert state.token_requests == []
        assert not self.token_file.exists()
        assert "Traceback" not in completed.stderr
        self.assert_credentials_absent(completed)

    def test_logout_failure_does_not_handoff_the_scoped_token(self) -> None:
        state = ServerState()
        state.logout_status = 500
        with fake_servers(state):
            completed = self.run_subject(state)
        assert completed.returncode != 0
        assert not self.token_file.exists()
        assert len(state.token_requests) == 1
        assert state.logout_calls == 1
        self.assert_credentials_absent(completed)


if __name__ == "__main__":
    unittest.main()
