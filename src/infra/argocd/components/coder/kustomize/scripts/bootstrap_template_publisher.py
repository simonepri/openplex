"""Mints local Coder template publisher token via OIDC before template reconciliation."""

from __future__ import annotations

import html
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from http import HTTPStatus
from http.cookiejar import CookieJar
from typing import IO, TYPE_CHECKING, override

from coder_api import BootstrapError, CoderAPI

if TYPE_CHECKING:
    from collections.abc import Sequence
    from http.client import HTTPMessage

TOKEN_NAME = "local-template-reconciler"
TOKEN_LIFETIME = 60 * 60 * 1_000_000_000
TOKEN_SCOPES = [
    "coder:templates.author",
    "coder:templates.build",
    "organization:read",
    "user:read",
]


@dataclass(frozen=True)
class OidcCredentials:
    username: str
    password: str


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


def origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise BootstrapError(f"Invalid bootstrap URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


class AllowedBrowserRedirects(urllib.request.HTTPRedirectHandler):
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
        target_origin = origin(newurl)
        if target_origin not in self.allowed_origins:
            raise BootstrapError(
                f"OIDC browser flow attempted off-target redirect to {target_origin}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def require_https(url: str, *, allow_insecure: bool) -> None:
    if urllib.parse.urlsplit(url).scheme != "https" and not allow_insecure:
        raise BootstrapError("OIDC bootstrap endpoints must use HTTPS")


def _start_browser_flow(opener: urllib.request.OpenerDirector, coder_url: str) -> tuple[str, str]:
    login_entrypoint = urllib.parse.urljoin(
        f"{coder_url.rstrip('/')}/",
        "api/v2/users/oidc/callback?redirect=%2F",
    )
    try:
        with opener.open(login_entrypoint, timeout=20) as response:
            res_url = response.url
            res_html = response.read(256 * 1024).decode()
            assert isinstance(res_url, str)
            assert isinstance(res_html, str)
            return res_url, res_html
    except BootstrapError:
        raise
    except (UnicodeDecodeError, urllib.error.HTTPError, urllib.error.URLError) as error:
        raise BootstrapError("Coder did not start the OIDC browser flow") from error


def _submit_login_form(
    opener: urllib.request.OpenerDirector,
    login_action: str,
    fields: dict[str, str],
) -> None:
    request = urllib.request.Request(
        login_action,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(request, timeout=20) as response:
            response.read(64 * 1024)
    except BootstrapError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        raise BootstrapError("OIDC fixture authentication was rejected") from error


def browser_login(
    coder_url: str,
    issuer_url: str,
    credentials: OidcCredentials,
    *,
    handlers: Sequence[urllib.request.BaseHandler] = (),
) -> str:
    cookie_jar = CookieJar()
    allowed_origins = {origin(coder_url), origin(issuer_url)}
    opener = urllib.request.build_opener(
        *handlers,
        urllib.request.HTTPCookieProcessor(cookie_jar),
        AllowedBrowserRedirects(allowed_origins),
    )
    login_url, login_html = _start_browser_flow(opener, coder_url)

    issuer_origin = origin(issuer_url)
    if origin(login_url) != issuer_origin:
        raise BootstrapError("Coder did not redirect to the declared OIDC issuer")
    parser = LoginFormParser()
    parser.feed(login_html)
    if not parser.action:
        raise BootstrapError("OIDC issuer did not provide its login form")
    login_action = urllib.parse.urljoin(login_url, parser.action)
    if origin(login_action) != issuer_origin:
        raise BootstrapError("OIDC login form attempted to leave its issuer")

    fields = dict(parser.fields)
    fields.update({"login": credentials.username, "password": credentials.password})
    _submit_login_form(opener, login_action, fields)

    sessions = [cookie.value for cookie in cookie_jar if cookie.name == "coder_session_token"]
    if len(sessions) != 1 or not sessions[0]:
        raise BootstrapError("OIDC flow did not create one Coder session")
    return sessions[0]


def require_owner(api: CoderAPI, username: str) -> None:
    _, user_value = api.request(
        "GET",
        "/api/v2/users/me",
        accepted={200},
    )
    if not isinstance(user_value, dict):
        raise BootstrapError("Coder returned an invalid current user")
    roles = user_value.get("roles")
    if not isinstance(roles, list):
        raise BootstrapError("Coder returned invalid current-user roles")
    role_names = {role.get("name") for role in roles if isinstance(role, dict)}
    if (
        user_value.get("email") != username
        or user_value.get("login_type") != "oidc"
        or "owner" not in role_names
    ):
        raise BootstrapError("local OIDC fixture is not the Coder owner")


def mint_scoped_token(coder_url: str, session_token: str, username: str) -> str:
    api = CoderAPI(coder_url, session_token)
    require_owner(api, username)
    token_name = urllib.parse.quote(TOKEN_NAME, safe="")
    status, old_token = api.request(
        "GET",
        f"/api/v2/users/me/keys/tokens/{token_name}",
        accepted={200, 404},
    )
    if status == HTTPStatus.OK:
        if not isinstance(old_token, dict) or not isinstance(old_token.get("id"), str):
            raise BootstrapError("Coder returned an invalid existing publisher token")
        token_id = urllib.parse.quote(old_token["id"], safe="")
        api.request(
            "DELETE",
            f"/api/v2/users/me/keys/{token_id}",
            accepted={204},
        )

    _, token_value = api.request(
        "POST",
        "/api/v2/users/me/keys/tokens",
        payload={
            "lifetime": TOKEN_LIFETIME,
            "scopes": TOKEN_SCOPES,
            "token_name": TOKEN_NAME,
        },
        accepted={201},
    )
    if not isinstance(token_value, dict) or not isinstance(token_value.get("key"), str):
        raise BootstrapError("Coder did not return the scoped publisher token")
    token = token_value["key"]
    if not isinstance(token, str) or not token or any(character.isspace() for character in token):
        raise BootstrapError("Coder returned an invalid scoped publisher token")
    return token


def logout(
    coder_url: str,
    session_token: str,
    *,
    handlers: Sequence[urllib.request.BaseHandler] = (),
) -> None:
    CoderAPI(coder_url, session_token, handlers=handlers).request(
        "POST",
        "/api/v2/users/logout",
        accepted={200},
    )


def write_token(path: str, token: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o440)
    except OSError as error:
        raise BootstrapError("scoped token file could not be created") from error
    try:
        encoded = token.encode()
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fchmod(descriptor, 0o440)
    except OSError as error:
        raise BootstrapError("scoped token file could not be written") from error
    finally:
        os.close(descriptor)
