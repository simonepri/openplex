"""Mints local Coder automation tokens through the Coder API and synchronizes them into secret-records."""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http import HTTPStatus
from http.cookiejar import CookieJar
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from collections.abc import Sequence
    from http.client import HTTPMessage

from infra.tools.cloud_emulator import runtime

TOKEN_NAME = "coder-automation-token"
TOKEN_LIFETIME = 10 * 365 * 24 * 60 * 60 * 1_000_000_000  # 10 years in nanoseconds
TOKEN_SCOPES = [
    "coder:templates.author",
    "coder:templates.build",
    "coder:workspaces.operate",
    "organization:read",
    "user:read",
    "workspace:read",
    "workspace:start",
    "workspace:stop",
    "workspace:update",
]
DEFAULT_USERNAME = "ops@local.internal"
DEFAULT_PASSWORD = "password"
DEFAULT_CONTROL_CLUSTER = "ctrl-eaws-lh1"
DEFAULT_TIMEOUT = 300


class CoderTokenError(Exception):
    """Report an automation token provisioning or synchronization failure."""


class RejectAPIRedirects(urllib.request.HTTPRedirectHandler):
    """Prevent an API redirect from forwarding authentication headers."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""
        self.fields: dict[str, str] = {}

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "form" and "action" in attr_map:
            self.action = html.unescape(attr_map["action"])
        if tag == "input" and attr_map.get("type", "").lower() not in {"submit", "button"}:
            name = attr_map.get("name")
            if name:
                self.fields[name] = attr_map.get("value", "")


def get_origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise CoderTokenError(f"Invalid URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


class AllowedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origins: set[str]) -> None:
        super().__init__()
        self.allowed_origins = allowed_origins

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        target_origin = get_origin(newurl)
        if target_origin not in self.allowed_origins:
            raise CoderTokenError(
                f"OIDC browser flow attempted off-target redirect to {target_origin}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def browser_login(
    coder_url: str,
    issuer_url: str,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    *,
    handlers: Sequence[urllib.request.BaseHandler] = (),
) -> str:
    cookie_jar = CookieJar()
    allowed_origins = {get_origin(coder_url), get_origin(issuer_url)}
    opener = urllib.request.build_opener(
        *handlers,
        urllib.request.HTTPCookieProcessor(cookie_jar),
        AllowedRedirects(allowed_origins),
    )
    login_entrypoint = urllib.parse.urljoin(
        f"{coder_url.rstrip('/')}/",
        "api/v2/users/oidc/callback?redirect=%2F",
    )
    try:
        with opener.open(login_entrypoint, timeout=20) as response:
            login_url = response.url
            login_html = response.read(256 * 1024).decode()
    except (UnicodeDecodeError, urllib.error.HTTPError, urllib.error.URLError) as error:
        raise CoderTokenError(f"Coder did not start the OIDC browser flow: {error}") from error

    parser = LoginFormParser()
    parser.feed(login_html)
    if not parser.action:
        raise CoderTokenError("OIDC issuer did not provide a login form")
    login_action = urllib.parse.urljoin(login_url, parser.action)

    fields = dict(parser.fields)
    fields.update({"login": username, "password": password})
    request = urllib.request.Request(
        login_action,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(request, timeout=20) as response:
            response.read(64 * 1024)
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        raise CoderTokenError(f"OIDC authentication was rejected: {error}") from error

    sessions = [cookie.value for cookie in cookie_jar if cookie.name == "coder_session_token"]
    if not sessions or sessions[0] is None:
        raise CoderTokenError("OIDC flow did not create a Coder session token")
    return sessions[0]


class CoderClient:
    def __init__(
        self,
        coder_url: str,
        session_token: str,
        *,
        handlers: Sequence[urllib.request.BaseHandler] = (),
    ) -> None:
        self.coder_url = coder_url.rstrip("/")
        self.session_token = session_token
        self.opener = urllib.request.build_opener(*handlers, RejectAPIRedirects())

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        accepted: set[int],
    ) -> tuple[int, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        url = urllib.parse.urljoin(f"{self.coder_url}/", path.lstrip("/"))
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Coder-Session-Token": self.session_token,
            },
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                status = response.status
                raw = response.read(1024 * 1024)
        except urllib.error.HTTPError as error:
            if error.code in accepted:
                with error:
                    raw = error.read(1024 * 1024)
                status = error.code
            else:
                raise CoderTokenError(f"Coder API {path} returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise CoderTokenError(f"Coder API {path} unreachable: {error}") from error

        if status not in accepted:
            raise CoderTokenError(f"Coder API {path} returned unexpected status {status}")
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError as error:
            raise CoderTokenError(f"Coder API {path} returned invalid JSON") from error


def mint_token_via_api(
    coder_url: str,
    issuer_url: str,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    *,
    token_name: str = TOKEN_NAME,
    lifetime: int = TOKEN_LIFETIME,
    scopes: list[str] | None = None,
    handlers: Sequence[urllib.request.BaseHandler] = (),
) -> str:
    """Authenticate through Dex OIDC and mint a Coder automation token."""
    session = browser_login(coder_url, issuer_url, username, password, handlers=handlers)
    client = CoderClient(coder_url, session, handlers=handlers)
    token_scopes = scopes or TOKEN_SCOPES

    # Check for and clean up any pre-existing token with the same name
    quoted_name = urllib.parse.quote(token_name, safe="")
    status, old_token = client.request(
        "GET",
        f"/api/v2/users/me/keys/tokens/{quoted_name}",
        accepted={200, 404},
    )
    if status == HTTPStatus.OK and isinstance(old_token, dict) and "id" in old_token:
        token_id = urllib.parse.quote(str(old_token["id"]), safe="")
        client.request("DELETE", f"/api/v2/users/me/keys/{token_id}", accepted={204})

    # Mint the new automation token
    _, token_data = client.request(
        "POST",
        "/api/v2/users/me/keys/tokens",
        payload={
            "lifetime": lifetime,
            "scopes": token_scopes,
            "token_name": token_name,
        },
        accepted={201},
    )
    if not isinstance(token_data, dict):
        raise CoderTokenError("Coder did not return a valid token key")
    token = token_data.get("key")
    if not isinstance(token, str):
        raise CoderTokenError("Coder did not return a valid token key")

    # Log out session
    with contextlib.suppress(Exception):
        client.request("POST", "/api/v2/users/logout", accepted={200})

    return token


def ensure_secret_record(
    context: str,
    token: str,
    *,
    namespace: str = "secret-records",
    secret_name: str = "coder-automation-token",  # ruff: ignore[hardcoded-password-default]
) -> None:
    """Write or update the automation token in the local cluster's secret records."""
    patch = {"stringData": {"token": token}}
    cmd = [
        "kubectl",
        "--context",
        context,
        "-n",
        namespace,
        "patch",
        "secret",
        secret_name,
        "--type=merge",
        "-p",
        json.dumps(patch),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        create_cmd = [
            "kubectl",
            "--context",
            context,
            "-n",
            namespace,
            "create",
            "secret",
            "generic",
            secret_name,
            f"--from-literal=token={token}",
            "--dry-run=client",
            "-o",
            "yaml",
        ]
        manifest = subprocess.check_output(create_cmd, text=True)
        apply_cmd = [
            "kubectl",
            "--context",
            context,
            "-n",
            namespace,
            "apply",
            "-f",
            "-",
        ]
        subprocess.run(apply_cmd, input=manifest, check=True, capture_output=True, text=True)


def wait_for_coder(context: str, timeout: int = DEFAULT_TIMEOUT) -> None:
    """Wait for Coder deployment in the coder namespace to be rolled out."""
    cmd = [
        "kubectl",
        "--context",
        context,
        "-n",
        "coder",
        "rollout",
        "status",
        "deployment/coder",
        f"--timeout={timeout}s",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise CoderTokenError(
            f"Timed out waiting for Coder deployment to be ready: {error.stderr}"
        ) from error


def make_ssl_context(ca_path: Path | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ca_path and ca_path.is_file():
        ctx.load_verify_locations(cafile=str(ca_path))
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def reconcile(
    root: Path,
    *,
    context: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    coder_url: str | None = None,
    dex_url: str | None = None,
    token: str | None = None,
) -> str:
    """Reconcile and write the Coder automation token into the local fleet's secret records."""
    manifest = runtime.load_local_deployment(root)
    control_cluster = context or runtime.control_cluster_record(manifest) or DEFAULT_CONTROL_CLUSTER

    if token is not None:
        minted_token = token
    else:
        wait_for_coder(control_cluster, timeout=timeout)
        ca_file = root / ".tmp/state/opentofu/local/pki/public/ca.crt"
        ssl_ctx = make_ssl_context(ca_file if ca_file.is_file() else None)
        handler = urllib.request.HTTPSHandler(context=ssl_ctx)

        c_url = coder_url or "https://coder.corp.local.internal"
        d_url = dex_url or "https://dex.corp.local.internal"

        minted_token = mint_token_via_api(
            c_url,
            d_url,
            username=username,
            password=password,
            handlers=[handler],
        )

    ensure_secret_record(control_cluster, minted_token)
    return minted_token


def main(args: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint and publish Coder automation token to secret-records"
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Kubernetes cluster context (default: derived from local deployment or ctrl-eaws-lh1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Maximum seconds to wait for Coder readiness (default: 300)",
    )
    parser.add_argument(
        "--coder-url",
        default=None,
        help="Coder base URL (default: https://coder.corp.local.internal)",
    )
    parser.add_argument(
        "--dex-url",
        default=None,
        help="Dex issuer URL (default: https://dex.corp.local.internal)",
    )
    parser.add_argument(
        "--username",
        default=DEFAULT_USERNAME,
        help=f"OIDC bootstrap username (default: {DEFAULT_USERNAME})",
    )
    parser.add_argument(
        "--password",
        default=DEFAULT_PASSWORD,
        help="OIDC bootstrap password",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Pre-minted Coder token to synchronize directly into secret-records",
    )
    parsed = parser.parse_args(args)

    repo_root = Path(__file__).resolve()
    while (
        repo_root.parent != repo_root
        and not (repo_root / "WORKSPACE").is_file()
        and not (repo_root / "MODULE.bazel").is_file()
    ):
        repo_root = repo_root.parent

    try:
        token = reconcile(
            repo_root,
            context=parsed.context,
            timeout=parsed.timeout,
            username=parsed.username,
            password=parsed.password,
            coder_url=parsed.coder_url,
            dex_url=parsed.dex_url,
            token=parsed.token,
        )
        print(
            f"Successfully synchronized Coder automation token into secret-records ({len(token)} chars)."
        )
        return 0
    except Exception as error:
        print(f"Failed to synchronize Coder automation token: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
