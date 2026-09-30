"""Serves Dragonfly Console through a constrained OIDC-to-Manager bridge."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from collections.abc import Mapping

HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300
PERMISSION_ENTRY_LEN = 3

MANAGER_HOST = os.environ.get("MANAGER_HOST", "dragonfly-manager")
MANAGER_PORT = int(os.environ.get("MANAGER_PORT", "8080"))
ROOT_USER_ID = 1
SESSION_SECONDS = 15 * 60
SSO_ROLE = "console-readonly"
ROLE_PERMISSIONS = frozenset({
    ("audits", "read"),
    ("clusters", "read"),
    ("jobs", "read"),
    ("peers", "read"),
    ("persistent-cache-tasks", "read"),
    ("scheduler-features", "read"),
    ("schedulers", "read"),
    ("seed-peers", "read"),
    ("users", "read"),
})
READ_COLLECTIONS = frozenset(permission[0] for permission in ROLE_PERMISSIONS - {("users", "read")})
RESOURCE_PATH = re.compile(
    rf"^/api/v1/(?:{'|'.join(re.escape(name) for name in sorted(READ_COLLECTIONS))})(?:/[A-Za-z0-9_-]+)?$"
)
SPA_PATHS = (
    re.compile(r"^/$"),
    re.compile(r"^/(?:audit|clusters|profile)$"),
    re.compile(
        r"^/clusters/(?!new(?:/|$))[A-Za-z0-9_-]+(?:/peers|/schedulers(?:/[A-Za-z0-9_-]+)?)?$"
    ),
    re.compile(r"^/gc/[A-Za-z0-9_-]+$"),
    re.compile(r"^/jobs/preheats(?:/(?!new$)[A-Za-z0-9_-]+)?$"),
    re.compile(
        r"^/resource/persistent-cache-task(?:/clusters/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?)?$"
    ),
    re.compile(r"^/resource/task/[A-Za-z0-9_-]+$"),
    re.compile(r"^/resource/task/executions/[A-Za-z0-9_-]+$"),
    re.compile(r"^/users$"),
)
STATIC_PATH = re.compile(
    r"^/(?:static|fonts)/[A-Za-z0-9_./-]+$|^/(?:asset-manifest\.json|manifest\.json|favicon/favicon\.ico)$"
)
FORWARDED_HEADERS = frozenset({
    "accept",
    "accept-encoding",
    "accept-language",
    "if-modified-since",
    "if-none-match",
    "range",
    "user-agent",
})
RESPONSE_HEADERS = frozenset({
    "cache-control",
    "content-disposition",
    "content-encoding",
    "content-language",
    "content-type",
    "etag",
    "expires",
    "last-modified",
    "location",
    "vary",
})
IDENTITY_MUTATION_LOCK = threading.Lock()


class ManagerError(Exception):
    """Report a Manager response that cannot satisfy the SSO contract."""


@dataclass(frozen=True)
class Identity:
    """Carry the immutable OIDC key and mutable verified display claims."""

    issuer: str
    subject: str
    preferred_username: str
    email: str


_identity_cache: dict[tuple[str, str], tuple[int, float, str, str]] = {}


def identity_digest(issuer: str, subject: str) -> str:
    """Return a collision-resistant digest of an unambiguous OIDC key."""
    encoded = json.dumps([issuer, subject], separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def mapped_email(identity: Identity) -> str:
    """Return the email used by the bridge before human profiles were displayed."""
    return f"{identity_digest(identity.issuer, identity.subject)}@oidc.invalid"


def profile_marker(email: str) -> str:
    """Return the marker written by the legacy bridge for migration lookup."""
    return f"oidc-profile-email:{email}"


def subject_marker(identity: Identity) -> str:
    """Return the durable issuer-and-subject binding stored on the user row."""
    return f"oidc-subject-sha256:{identity_digest(identity.issuer, identity.subject)}"


FORWARDED_IDENTITY_HEADERS = frozenset({
    "x-forwarded-user",
    "x-forwarded-preferred-username",
    "x-forwarded-email",
    "x-forwarded-groups",
})


def _matches_proxy_entry(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, entry: str) -> bool:
    try:
        if "/" in entry:
            return ip in ipaddress.ip_network(entry, strict=False)
        return ip == ipaddress.ip_address(entry)
    except ValueError:
        return False


def is_trusted_proxy(client_ip: str | None) -> bool:
    """Determine whether the client address is an authorized proxy."""
    if client_ip is None or client_ip in {"127.0.0.1", "::1", "localhost"}:
        return True
    trusted_env = os.environ.get("TRUSTED_PROXIES", "").strip()
    if not trusted_env:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    entries = (e.strip() for e in trusted_env.split(",") if e.strip())
    return any(_matches_proxy_entry(ip, entry) for entry in entries)


def verify_forwarded_headers(headers: Mapping[str, str], key: str | None) -> bool:
    """Validate cryptographic signature on forwarded identity headers."""
    if not key:
        return False
    normalized = {k.lower(): v for k, v in headers.items()}
    signature = normalized.get("x-forwarded-signature") or normalized.get("x-signature")
    if not signature:
        return False
    data = ":".join(normalized.get(name, "") for name in sorted(FORWARDED_IDENTITY_HEADERS))
    expected = hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def is_trusted_forward_request(
    headers: Mapping[str, str],
    client_ip: str | None,
    signing_key: str | None,
) -> bool:
    """Reject caller-supplied forward headers unless verified or from the trusted proxy."""
    has_identity_headers = any(name.lower() in FORWARDED_IDENTITY_HEADERS for name in headers)
    if not has_identity_headers:
        return True
    if is_trusted_proxy(client_ip):
        return True
    return verify_forwarded_headers(headers, signing_key)


def asserted_identity(
    headers: Mapping[str, str],
    issuer: str,
    operator_group: str,
    *,
    trusted: bool = True,
) -> Identity:
    """Parse the verified claims forwarded by Envoy Gateway."""
    if not trusted:
        raise PermissionError("untrusted forward headers")
    normalized = {name.lower(): value for name, value in headers.items()}
    subject = normalized.get("x-forwarded-user", "")
    preferred_username = normalized.get("x-forwarded-preferred-username", "")
    email = normalized.get("x-forwarded-email", "")
    try:
        groups = json.loads(
            base64.b64decode(normalized.get("x-forwarded-groups", ""), validate=True)
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise PermissionError("invalid Dragonfly operator groups") from error
    if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
        raise PermissionError("invalid Dragonfly operator groups")
    valid_name = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", preferred_username))
    valid_user = valid_name and ("@" in email) and (operator_group in groups)
    if not (issuer and subject and email and valid_user):
        raise PermissionError("missing required Dragonfly operator identity")
    return Identity(
        issuer=issuer,
        subject=subject,
        preferred_username=preferred_username,
        email=email,
    )


def encode_segment(value: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    )
    return encoded.rstrip(b"=").decode()


def user_token(user_id: int, key: str, cell: str, now: int | None = None) -> str:
    """Mint the short Manager session accepted only by this cell's JWT key."""
    issued_at = int(time.time()) if now is None else now
    header = encode_segment({"alg": "HS256", "typ": "JWT"})
    claims = encode_segment({
        "cell": cell,
        "exp": issued_at + SESSION_SECONDS,
        "id": user_id,
        "orig_iat": issued_at,
    })
    unsigned = f"{header}.{claims}"
    signature = base64.urlsafe_b64encode(
        hmac.new(key.encode(), unsigned.encode(), hashlib.sha256).digest()
    ).rstrip(b"=")
    return f"{unsigned}.{signature.decode()}"


def request_manager(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection(MANAGER_HOST, MANAGER_PORT, timeout=10)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def response_json(status: int, payload: bytes) -> object:
    if status < HTTP_OK or status >= HTTP_MULTIPLE_CHOICES:
        raise ManagerError(f"Dragonfly Manager returned HTTP {status}")
    return json.loads(payload)


def administrative_token(key: str, cell: str) -> str:
    """Mint the internal session for Dragonfly's fixed root user."""
    return user_token(ROOT_USER_ID, key, cell)


def ensure_role(token: str) -> None:
    """Create the exact read-only role, or reject any pre-existing drift."""
    status, payload, _ = request_manager("GET", f"/api/v1/roles/{SSO_ROLE}", token=token)
    permissions = response_json(status, payload)
    if not isinstance(permissions, list):
        raise ManagerError("Dragonfly SSO role permissions have drifted")
    if permissions == []:
        status, _, _ = request_manager(
            "POST",
            "/api/v1/roles",
            {
                "permissions": [
                    {"action": action, "object": resource}
                    for resource, action in sorted(ROLE_PERMISSIONS)
                ],
                "role": SSO_ROLE,
            },
            token,
        )
        if status not in {HTTPStatus.OK, HTTPStatus.CREATED}:
            raise ManagerError(f"Dragonfly role creation returned HTTP {status}")
        status, payload, _ = request_manager("GET", f"/api/v1/roles/{SSO_ROLE}", token=token)
        permissions = response_json(status, payload)
        if not isinstance(permissions, list):
            raise ManagerError("Dragonfly SSO role permissions have drifted")
    actual = {
        (entry[1], entry[2])
        for entry in permissions
        if isinstance(entry, list) and len(entry) == PERMISSION_ENTRY_LEN and entry[0] == SSO_ROLE
    }
    if actual != ROLE_PERMISSIONS or len(actual) != len(permissions):
        raise ManagerError("Dragonfly SSO role permissions have drifted")


def find_user(identity: Identity, token: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"page": 1, "per_page": 10_000_000})
    status, payload, _ = request_manager("GET", f"/api/v1/users?{query}", token=token)
    users = response_json(status, payload)
    if not isinstance(users, list):
        raise ManagerError("Dragonfly identity lookup was ambiguous")
    legacy_email = mapped_email(identity)
    marker = subject_marker(identity)
    matches = [
        user for user in users if user.get("bio") == marker or user.get("email") == legacy_email
    ]
    conflicts = [
        user
        for user in users
        if user not in matches
        and (
            user.get("name") == identity.preferred_username
            or user.get("email") == identity.email
            or user.get("bio") == marker
            or user.get("bio") == profile_marker(identity.email)
        )
    ]
    if len(matches) > 1 or conflicts:
        raise ManagerError("Dragonfly profile is bound to another OIDC identity")
    return matches[0] if matches else None


def _create_user(identity: Identity, token: str) -> dict[str, Any]:
    expected_bio = subject_marker(identity)
    status, _, _ = request_manager(
        "POST",
        "/api/v1/users/signup",
        {
            "bio": expected_bio,
            "email": identity.email,
            "name": identity.preferred_username,
            "password": secrets.token_urlsafe(15)[:20],
        },
    )
    if status not in {HTTPStatus.OK, HTTPStatus.CREATED}:
        raise ManagerError(f"Dragonfly user creation returned HTTP {status}")
    user = find_user(identity, token)
    if not isinstance(user, dict):
        raise ManagerError("Dragonfly identity mapping did not create a user")
    return user


def _validate_and_sync_profile(user: dict[str, Any], identity: Identity, token: str) -> int:
    if user.get("state") != "enable":
        raise ManagerError("Dragonfly identity mapping conflicts with an existing user")
    user_id = user.get("id")
    if not isinstance(user_id, int) or user_id < 1:
        raise ManagerError("Dragonfly identity mapping returned an invalid user ID")
    profile = {
        "bio": subject_marker(identity),
        "email": identity.email,
        "name": identity.preferred_username,
    }
    if any(user.get(field) != value for field, value in profile.items()):
        status, _, _ = request_manager("PATCH", f"/api/v1/users/{user_id}", profile, token)
        if status not in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            raise ManagerError(f"Dragonfly profile update returned HTTP {status}")
    return user_id


def _sync_user_roles(user_id: int, token: str) -> None:
    status, payload, _ = request_manager("GET", f"/api/v1/users/{user_id}/roles", token=token)
    roles = response_json(status, payload)
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise ManagerError("Dragonfly returned an invalid role set")
    for role in roles:
        if role != SSO_ROLE:
            status, _, _ = request_manager(
                "DELETE",
                f"/api/v1/users/{user_id}/roles/{urllib.parse.quote(role, safe='')}",
                token=token,
            )
            if status not in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
                raise ManagerError(f"Dragonfly role removal returned HTTP {status}")
    if SSO_ROLE not in roles:
        status, _, _ = request_manager(
            "PUT", f"/api/v1/users/{user_id}/roles/{SSO_ROLE}", token=token
        )
        if status not in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            raise ManagerError(f"Dragonfly role assignment returned HTTP {status}")
    status, payload, _ = request_manager("GET", f"/api/v1/users/{user_id}/roles", token=token)
    final_roles = response_json(status, payload)
    if not isinstance(final_roles, list) or set(final_roles) != {SSO_ROLE} or len(final_roles) != 1:
        raise ManagerError("Dragonfly SSO user does not have the exact required role")


def ensure_user(identity: Identity, key: str, cell: str) -> int:
    """Resolve one enabled user with exactly the constrained SSO role."""
    token = administrative_token(key, cell)
    ensure_role(token)
    user = find_user(identity, token)
    if user is None:
        user = _create_user(identity, token)
    user_id = _validate_and_sync_profile(user, identity, token)
    _sync_user_roles(user_id, token)
    return user_id


def resolve_user(identity: Identity, signing_key: str, cell: str) -> int:
    """Cache one verified mapping for the lifetime of its short Manager session."""
    with IDENTITY_MUTATION_LOCK:
        now = time.monotonic()
        cache_key = (identity.issuer, identity.subject)
        cached = _identity_cache.get(cache_key)
        if (
            cached is not None
            and now < cached[1]
            and cached[2:] == (identity.preferred_username, identity.email)
        ):
            return cached[0]
        for cached_identity, (_, deadline, _, _) in list(_identity_cache.items()):
            if now >= deadline:
                del _identity_cache[cached_identity]
        user_id = ensure_user(identity, signing_key, cell)
        _identity_cache[cache_key] = (
            user_id,
            now + SESSION_SECONDS,
            identity.preferred_username,
            identity.email,
        )
        return user_id


def clear_identity_cache() -> None:
    """Clear cached identity mappings."""
    with IDENTITY_MUTATION_LOCK:
        _identity_cache.clear()


def _is_valid_target_structure(target: urllib.parse.SplitResult) -> bool:
    if target.scheme or target.netloc or target.fragment:
        return False
    if "%" in target.path or ";" in target.query:
        return False
    segments = target.path.split("/")[1:]
    return not (target.path != "/" and any(segment in {"", ".", ".."} for segment in segments))


def canonical_target(raw_target: str) -> urllib.parse.SplitResult | None:
    """Parse one unambiguous origin-form request target."""
    if (
        not raw_target.startswith("/")
        or raw_target.startswith("//")
        or any(character in raw_target for character in ("\\", "\r", "\n", "\t"))
    ):
        return None
    try:
        target = urllib.parse.urlsplit(raw_target)
    except ValueError:
        return None
    if not _is_valid_target_structure(target):
        return None
    return target


def request_headers_safe(headers: Mapping[str, str] | None) -> bool:
    """Reject alternate credentials that could conflict with bridge identity."""
    if headers is None:
        return True
    normalized = {name.lower(): value for name, value in headers.items()}
    if normalized.get("authorization"):
        return False
    cookie = normalized.get("cookie", "")
    credential_names = {"access_token", "authorization", "token"}
    return not any(
        part.partition("=")[0].strip().lower() in credential_names
        for part in cookie.split(";")
        if "=" in part
    )


def allowed_request(
    method: str, raw_target: str, user_id: int, headers: Mapping[str, str] | None = None
) -> bool:
    """Accept only Console static reads and the documented read API surface."""
    target = canonical_target(raw_target)
    if target is None or not request_headers_safe(headers) or method != "GET":
        return False
    if any(
        key.lower() in {"access_token", "token"}
        for key, _ in urllib.parse.parse_qsl(target.query, keep_blank_values=True)
    ):
        return False
    if target.path == "/api/v1/users" or target.path in {
        f"/api/v1/users/{user_id}",
        f"/api/v1/users/{user_id}/roles",
    }:
        return True
    return (
        bool(RESOURCE_PATH.fullmatch(target.path))
        or any(pattern.fullmatch(target.path) for pattern in SPA_PATHS)
        or bool(STATIC_PATH.fullmatch(target.path))
    )


def manager_target(raw_target: str) -> str:
    """Map an allowed Console route to the Manager's SPA entry point."""
    target = canonical_target(raw_target)
    if target is not None and any(pattern.fullmatch(target.path) for pattern in SPA_PATHS):
        return "/"
    return raw_target


class Handler(BaseHTTPRequestHandler):
    """Authenticate trusted headers before serving the constrained Console."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.handle_request()

    def do_POST(self) -> None:
        self.handle_request()

    def do_PUT(self) -> None:
        self.handle_request()

    def do_PATCH(self) -> None:
        self.handle_request()

    def do_DELETE(self) -> None:
        self.handle_request()

    def _authenticate_and_get_token(self) -> str | None:
        headers = dict(self.headers.items())
        client_ip = self.client_address[0] if getattr(self, "client_address", None) else None
        signing_key = os.environ.get("JWT_KEY")
        if not is_trusted_forward_request(headers, client_ip, signing_key):
            raise PermissionError("untrusted forward headers from unverified source")
        identity = asserted_identity(
            headers, os.environ["OIDC_ISSUER"], os.environ["OPERATOR_GROUP"]
        )
        user_id = resolve_user(identity, os.environ["JWT_KEY"], os.environ["CELL_ID"])
        if not allowed_request(self.command, self.path, user_id, headers):
            self.send_error(HTTPStatus.FORBIDDEN)
            return None
        return user_token(user_id, os.environ["JWT_KEY"], os.environ["CELL_ID"])

    def handle_request(self) -> None:
        if (
            self.command == "POST"
            and urllib.parse.urlsplit(self.path).path == "/api/v1/users/signout"
        ):
            self.sign_out()
            return
        try:
            token = self._authenticate_and_get_token()
            if token is not None:
                self.proxy(token)
        except PermissionError:
            self.send_error(HTTPStatus.UNAUTHORIZED)
        except (KeyError, ManagerError, OSError, ValueError):
            self.send_error(HTTPStatus.BAD_GATEWAY)

    def sign_out(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("set-cookie", "jwt=; Max-Age=0; Path=/; Secure; SameSite=Lax")
        cookie_name = os.environ.get("OAUTH_COOKIE_NAME", "_dragonfly_console")
        self.send_header(
            "set-cookie",
            f"{cookie_name}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax",
        )
        self.send_header("content-length", "0")
        self.end_headers()

    def proxy(self, token: str) -> None:
        headers = {
            name: value for name, value in self.headers.items() if name.lower() in FORWARDED_HEADERS
        }
        headers["authorization"] = f"Bearer {token}"
        connection = http.client.HTTPConnection(MANAGER_HOST, MANAGER_PORT, timeout=30)
        try:
            connection.request("GET", manager_target(self.path), headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() in RESPONSE_HEADERS:
                    self.send_header(name, value)
            self.send_header(
                "set-cookie",
                f"jwt={token}; Max-Age={SESSION_SECONDS}; Path=/; Secure; SameSite=Lax",
            )
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()

    @override
    def log_message(self, format: str, *args: Any) -> None:
        del format, args


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
