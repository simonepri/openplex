"""Protects Dragonfly SSO bridge identity, role, route, and session boundaries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http import HTTPStatus
from typing import override
from unittest import mock

import sso

SHA256_HEX_LEN = 64
USER_ID_7 = 7
USER_ID_8 = 8
USER_ID_9 = 9
TWO_CALLS = 2
TWO_COOKIES = 2


def decode_segment(segment: str) -> dict[str, object]:
    result = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    assert isinstance(result, dict)
    return {str(k): v for k, v in result.items()}


class AdministrativeSessionTest(unittest.TestCase):
    @staticmethod
    def test_internal_administration_uses_the_seeded_root_identity() -> None:
        token = sso.administrative_token("cell-key", "ctrl-eaws-lh1")
        claims = decode_segment(token.split(".")[1])
        assert claims["id"] == sso.ROOT_USER_ID
        assert claims["cell"] == "ctrl-eaws-lh1"


class IdentityMappingTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        sso.clear_identity_cache()

    @staticmethod
    def test_identity_includes_issuer_and_full_collision_resistant_digest() -> None:
        identity = sso.Identity("https://issuer.example", "subject", "user", "user@example.com")
        expected = hashlib.sha256(b'["https://issuer.example","subject"]').hexdigest()
        assert sso.mapped_email(identity) == f"{expected}@oidc.invalid"
        assert len(sso.mapped_email(identity).split("@", 1)[0]) == SHA256_HEX_LEN
        migrated = sso.Identity("https://new-issuer.example", "subject", "user", "user@example.com")
        assert sso.mapped_email(identity) != sso.mapped_email(migrated)

    @staticmethod
    def test_operator_group_is_required() -> None:
        headers = {
            "X-Forwarded-User": "immutable-subject",
            "X-Forwarded-Preferred-Username": "operator",
            "X-Forwarded-Email": "operator@unit.test",
            "X-Forwarded-Groups": base64.b64encode(b'["team:examples"]').decode(),
        }
        error_raised = False
        try:
            sso.asserted_identity(headers, "https://dex.example", "operators")
        except PermissionError:
            error_raised = True
        assert error_raised
        headers["X-Forwarded-Groups"] = base64.b64encode(b'["team:examples","operators"]').decode()
        assert sso.asserted_identity(headers, "https://dex.example", "operators") == sso.Identity(
            "https://dex.example", "immutable-subject", "operator", "operator@unit.test"
        )

    def test_envoy_headers_preserve_identity_and_reject_malformed_or_nonoperator_groups(
        self,
    ) -> None:
        headers = {
            "x-forwarded-user": "immutable-subject",
            "x-forwarded-preferred-username": "operator",
            "x-forwarded-email": "operator@unit.test",
            "x-forwarded-groups": base64.b64encode(b'["team:examples","operators"]').decode(),
        }
        assert sso.asserted_identity(headers, "https://dex.example", "operators") == sso.Identity(
            "https://dex.example", "immutable-subject", "operator", "operator@unit.test"
        )
        invalid = [
            "",
            "operators",
            '["operators"]',
            "%%%",
            base64.b64encode(b"not-json").decode(),
            base64.b64encode(b'{"operators":true}').decode(),
            base64.b64encode(b'["operators",1]').decode(),
            base64.b64encode(b'["team:examples"]').decode(),
        ]
        for groups in invalid:
            with self.subTest(groups=groups), self.assertRaises(PermissionError):
                sso.asserted_identity(
                    dict(headers, **{"x-forwarded-groups": groups}),
                    "https://dex.example",
                    "operators",
                )

    @staticmethod
    @mock.patch.object(sso, "request_manager")
    def test_issuer_migration_with_same_profile_fails_closed(request_manager: mock.Mock) -> None:
        identity = sso.Identity("https://new.example", "subject", "operator", "operator@unit.test")
        request_manager.return_value = (
            200,
            json.dumps([
                {
                    "bio": sso.profile_marker(identity.email),
                    "email": sso.mapped_email(
                        sso.Identity(
                            "https://old.example",
                            "subject",
                            "operator",
                            identity.email,
                        )
                    ),
                }
            ]).encode(),
            {},
        )
        error_msg = ""
        try:
            sso.find_user(identity, "root-token")
        except sso.ManagerError as error:
            error_msg = str(error)
        assert "another OIDC identity" in error_msg

    @staticmethod
    @mock.patch.object(sso, "request_manager")
    def test_existing_native_email_for_another_subject_fails_closed(
        request_manager: mock.Mock,
    ) -> None:
        identity = sso.Identity("https://dex.example", "subject", "operator", "operator@unit.test")
        request_manager.return_value = (
            200,
            json.dumps([{"bio": "", "email": identity.email, "id": 4}]).encode(),
            {},
        )
        error_msg = ""
        try:
            sso.find_user(identity, "root-token")
        except sso.ManagerError as error:
            error_msg = str(error)
        assert "another OIDC identity" in error_msg

    @staticmethod
    @mock.patch.object(sso, "request_manager")
    def test_existing_username_for_another_subject_fails_closed(request_manager: mock.Mock) -> None:
        identity = sso.Identity("https://dex.example", "subject", "operator", "new@example.com")
        request_manager.return_value = (
            200,
            json.dumps([{"bio": "", "email": "other@example.com", "name": "operator"}]).encode(),
            {},
        )
        error_msg = ""
        try:
            sso.find_user(identity, "root-token")
        except sso.ManagerError as error:
            error_msg = str(error)
        assert "another OIDC identity" in error_msg

    @staticmethod
    @mock.patch.object(sso, "request_manager")
    def test_immutable_subject_marker_ignores_changed_display_claims(
        request_manager: mock.Mock,
    ) -> None:
        identity = sso.Identity("https://dex.example", "subject", "new-name", "new@example.com")
        user = {
            "bio": sso.subject_marker(identity),
            "email": "old@example.com",
            "id": USER_ID_7,
            "name": "old-name",
        }
        request_manager.return_value = (200, json.dumps([user]).encode(), {})
        assert sso.find_user(identity, "root-token") == user

    @staticmethod
    @mock.patch.object(sso, "request_manager")
    def test_exact_role_is_created_without_guest_permissions(request_manager: mock.Mock) -> None:
        expected = [
            [sso.SSO_ROLE, resource, action] for resource, action in sorted(sso.ROLE_PERMISSIONS)
        ]
        request_manager.side_effect = [
            (200, b"[]", {}),
            (200, b"", {}),
            (200, json.dumps(expected).encode(), {}),
        ]
        sso.ensure_role("root-token")
        body = request_manager.call_args_list[1].args[2]
        assert body["role"] == sso.SSO_ROLE
        assert {
            (item["object"], item["action"]) for item in body["permissions"]
        } == sso.ROLE_PERMISSIONS
        assert ("personal-access-tokens", "read") not in sso.ROLE_PERMISSIONS

    @staticmethod
    @mock.patch.object(sso, "administrative_token", return_value="root-token")
    @mock.patch.object(sso, "ensure_role")
    @mock.patch.object(sso, "request_manager")
    def test_mutable_email_updates_profile_without_changing_identity(
        request_manager: mock.Mock,
        mock_ensure_role: mock.Mock,
        mock_administrative_token: mock.Mock,
    ) -> None:
        del mock_ensure_role, mock_administrative_token
        identity = sso.Identity("https://dex.example", "subject", "operator", "new@example.com")
        immutable_email = sso.mapped_email(identity)
        user = {
            "bio": sso.profile_marker("old@example.com"),
            "email": immutable_email,
            "id": USER_ID_7,
            "name": "opaque-old-name",
            "state": "enable",
        }
        request_manager.side_effect = [
            (200, json.dumps([user]).encode(), {}),
            (200, b"{}", {}),
            (200, json.dumps([sso.SSO_ROLE]).encode(), {}),
            (200, json.dumps([sso.SSO_ROLE]).encode(), {}),
        ]
        assert sso.ensure_user(identity, "jwt-key", "ctrl-eaws-lh1") == USER_ID_7
        update = request_manager.call_args_list[1]
        assert update.args[:2] == ("PATCH", f"/api/v1/users/{USER_ID_7}")
        assert update.args[2] == {
            "bio": sso.subject_marker(identity),
            "email": "new@example.com",
            "name": "operator",
        }
        assert sso.mapped_email(identity) == immutable_email

    @staticmethod
    @mock.patch.object(sso, "administrative_token", return_value="root-token")
    @mock.patch.object(sso, "ensure_role")
    @mock.patch.object(sso, "request_manager")
    def test_role_escalation_is_removed_and_exact_role_rechecked(
        request_manager: mock.Mock,
        mock_ensure_role: mock.Mock,
        mock_administrative_token: mock.Mock,
    ) -> None:
        del mock_ensure_role, mock_administrative_token
        identity = sso.Identity("https://dex.example", "subject", "operator", "operator@unit.test")
        user = {
            "bio": sso.subject_marker(identity),
            "email": identity.email,
            "id": USER_ID_7,
            "name": identity.preferred_username,
            "state": "enable",
        }
        request_manager.side_effect = [
            (200, json.dumps([user]).encode(), {}),
            (200, b'["guest","root"]', {}),
            (204, b"", {}),
            (204, b"", {}),
            (204, b"", {}),
            (200, json.dumps([sso.SSO_ROLE]).encode(), {}),
        ]
        assert sso.ensure_user(identity, "jwt-key", "ctrl-eaws-lh1") == USER_ID_7
        calls = [(call.args[0], call.args[1]) for call in request_manager.call_args_list]
        assert ("DELETE", f"/api/v1/users/{USER_ID_7}/roles/root") in calls
        assert ("PUT", f"/api/v1/users/{USER_ID_7}/roles/{sso.SSO_ROLE}") in calls

    @staticmethod
    @mock.patch.object(sso, "ensure_user")
    def test_concurrent_requests_do_not_invalidate_root_sessions(ensure_user: mock.Mock) -> None:
        def manager_flow(_identity: sso.Identity, _key: str, _cell: str) -> int:
            time.sleep(0.01)
            return USER_ID_7

        ensure_user.side_effect = manager_flow
        identity = sso.Identity("https://dex.example", "subject", "operator", "operator@unit.test")
        with ThreadPoolExecutor(max_workers=4) as executor:
            user_ids = list(
                executor.map(
                    lambda _: sso.resolve_user(identity, "jwt-key", "ctrl-eaws-lh1"), range(4)
                )
            )
        assert user_ids == [USER_ID_7, USER_ID_7, USER_ID_7, USER_ID_7]
        ensure_user.assert_called_once_with(identity, "jwt-key", "ctrl-eaws-lh1")

    @staticmethod
    @mock.patch.object(sso, "ensure_user", side_effect=[USER_ID_7, USER_ID_8])
    def test_cached_identity_is_revalidated_after_session_expiry(ensure_user: mock.Mock) -> None:
        identity = sso.Identity("https://dex.example", "subject", "operator", "operator@unit.test")
        with mock.patch.object(
            sso.time,
            "monotonic",
            side_effect=[100.0, 101.0, 100.0 + sso.SESSION_SECONDS],
        ):
            assert sso.resolve_user(identity, "jwt-key", "ctrl-eaws-lh1") == USER_ID_7
            assert sso.resolve_user(identity, "jwt-key", "ctrl-eaws-lh1") == USER_ID_7
            assert sso.resolve_user(identity, "jwt-key", "ctrl-eaws-lh1") == USER_ID_8
        assert ensure_user.call_count == TWO_CALLS

    @staticmethod
    @mock.patch.object(sso, "ensure_user", side_effect=[USER_ID_7, USER_ID_9])
    def test_cached_users_remain_separated_by_oidc_identity(ensure_user: mock.Mock) -> None:
        first = sso.Identity("https://dex.example", "first", "first", "first@unit.test")
        second = sso.Identity("https://dex.example", "second", "second", "second@unit.test")
        assert sso.resolve_user(first, "jwt-key", "ctrl-eaws-lh1") == USER_ID_7
        assert sso.resolve_user(second, "jwt-key", "ctrl-eaws-lh1") == USER_ID_9
        assert sso.resolve_user(first, "jwt-key", "ctrl-eaws-lh1") == USER_ID_7
        assert ensure_user.call_count == TWO_CALLS


class RequestPolicyTest(unittest.TestCase):
    def test_read_only_console_allowlist_is_exact(self) -> None:
        allowed = [
            "/",
            "/static/js/main.01234567.js",
            "/fonts/MabryPro-Light.ttf",
            "/favicon/favicon.ico",
            "/asset-manifest.json",
            "/clusters/1/schedulers/2",
            "/resource/persistent-cache-task/clusters/1/task-2",
            "/api/v1/clusters",
            "/api/v1/clusters/1",
            "/api/v1/schedulers?cluster_id=1",
            "/api/v1/scheduler-features",
            "/api/v1/seed-peers",
            "/api/v1/peers",
            "/api/v1/jobs/abc-123",
            "/api/v1/persistent-cache-tasks",
            "/api/v1/audits",
            "/api/v1/users?page=1&per_page=10000000",
            f"/api/v1/users/{USER_ID_7}",
            f"/api/v1/users/{USER_ID_7}/roles",
        ]
        for target in allowed:
            with self.subTest(target=target):
                assert sso.allowed_request("GET", target, USER_ID_7)

    def test_pat_oapi_native_auth_and_administration_are_denied(self) -> None:
        denied = [
            ("GET", "/api/v1/personal-access-tokens"),
            ("GET", "/api/v1/personal-access-tokens/1"),
            ("GET", "/oapi/v1/jobs"),
            ("POST", "/api/v1/users/signin"),
            ("POST", "/api/v1/users/signup"),
            ("POST", f"/api/v1/users/{USER_ID_7}/reset_password"),
            ("GET", f"/api/v1/users/{USER_ID_8}"),
            ("GET", "/api/v1/roles"),
            ("GET", "/api/v1/permissions"),
            ("GET", "/api%2Fv1%2Fpersonal-access-tokens"),
            ("GET", "/metrics"),
            ("GET", "/assets/../api/v1/users"),
            ("GET", "/./api/v1/users"),
            ("GET", "//api/v1/users"),
            ("GET", "https://dragonfly.example/api/v1/clusters"),
            ("GET", "/%2e%2e/api/v1/clusters"),
            ("GET", "/static%2fjs/main.js"),
            ("GET", "/clusters/new"),
            ("GET", "/clusters/1/edit"),
            ("GET", "/developer/personal-access-tokens"),
            ("GET", "/developer/personal-access-tokens/new"),
            ("GET", "/jobs/preheats/new"),
            ("GET", "/signin"),
            ("GET", "/signup"),
            ("GET", "/users/new"),
            ("PATCH", "/api/v1/clusters/1"),
        ]
        for method, target in denied:
            with self.subTest(method=method, target=target):
                assert not sso.allowed_request(method, target, USER_ID_7)

    @mock.patch.object(sso, "resolve_user", return_value=USER_ID_7)
    def test_audit_page_requests_pass_oidc_bridge(self, resolve_user: mock.Mock) -> None:
        headers = Message()
        headers["x-forwarded-email"] = "operator@unit.test"
        headers["x-forwarded-groups"] = base64.b64encode(b'["operators"]').decode()
        headers["x-forwarded-preferred-username"] = "operator"
        headers["x-forwarded-user"] = "dex-subject"
        for target in (
            "/api/v1/users?page=1&per_page=10000000",
            "/api/v1/audits?page=1&per_page=10",
        ):
            with self.subTest(target=target):
                handler = object.__new__(sso.Handler)
                handler.command = "GET"
                handler.path = target
                handler.headers = headers
                handler.proxy = mock.Mock()
                handler.send_error = mock.Mock()
                with mock.patch.dict(
                    sso.os.environ,
                    {
                        "CELL_ID": "ctrl-eaws-lh1",
                        "JWT_KEY": "jwt-key",
                        "OIDC_ISSUER": "https://dex.unit.test",
                        "OPERATOR_GROUP": "operators",
                    },
                ):
                    handler.handle_request()
                handler.send_error.assert_not_called()
                handler.proxy.assert_called_once()
        assert resolve_user.call_count == TWO_CALLS

    def test_query_token_replay_is_denied_case_insensitively(self) -> None:
        for key in ("access_token", "ACCESS_TOKEN", "token"):
            with self.subTest(key=key):
                assert not sso.allowed_request("GET", f"/api/v1/clusters?{key}=stolen", USER_ID_7)
        assert not sso.allowed_request("GET", "/api/v1/clusters?x=1;access_token=x", USER_ID_7)

    @staticmethod
    def test_alternate_header_and_cookie_credentials_are_denied() -> None:
        assert not sso.allowed_request(
            "GET", "/api/v1/clusters", USER_ID_7, {"authorization": "Bearer stolen"}
        )
        assert not sso.allowed_request(
            "GET", "/api/v1/clusters", USER_ID_7, {"cookie": "access_token=stolen"}
        )
        assert sso.allowed_request(
            "GET",
            "/api/v1/clusters",
            USER_ID_7,
            {"Cookie": "jwt=console-session; _dragonfly_console=proxy-session"},
        )

    @staticmethod
    def test_browser_cookie_and_authorization_are_never_forwarded() -> None:
        assert "cookie" not in sso.FORWARDED_HEADERS
        assert "authorization" not in sso.FORWARDED_HEADERS

    def test_spa_routes_use_the_manager_entry_point(self) -> None:
        for target in ("/", "/clusters", "/clusters/1/schedulers/2"):
            with self.subTest(target=target):
                assert sso.manager_target(target) == "/"
        assert sso.manager_target("/api/v1/clusters?page=1") == "/api/v1/clusters?page=1"
        assert sso.manager_target("/static/js/main.js") == "/static/js/main.js"


class SessionTest(unittest.TestCase):
    @staticmethod
    def test_short_token_expiry_and_cross_user_binding() -> None:
        first = sso.user_token(41, "cell-a-key", "cell-a", now=1_000)
        second = sso.user_token(42, "cell-a-key", "cell-a", now=1_000)
        claims = decode_segment(first.split(".")[1])
        assert claims == {"cell": "cell-a", "exp": 1900, "id": 41, "orig_iat": 1000}
        assert first != second

    @staticmethod
    def test_cell_keys_cryptographically_separate_tokens() -> None:
        token = sso.user_token(41, "cell-a-key", "cell-a", now=1_000)
        header, claims, signature = token.split(".")
        wrong = base64.urlsafe_b64encode(
            hmac.new(b"cell-b-key", f"{header}.{claims}".encode(), hashlib.sha256).digest()
        ).rstrip(b"=")
        assert signature != wrong.decode()

    @staticmethod
    def test_signout_expires_manager_and_oauth_cookies_without_minting() -> None:
        handler = object.__new__(sso.Handler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        with mock.patch.dict(sso.os.environ, {"OAUTH_COOKIE_NAME": "_dragonfly_console"}):
            handler.sign_out()
        cookies = [
            call.args[1]
            for call in handler.send_header.call_args_list
            if call.args[0] == "set-cookie"
        ]
        assert len(cookies) == TWO_COOKIES
        assert all("Max-Age=0" in cookie for cookie in cookies)
        assert not any(
            "jwt=" in cookie and cookie != "jwt=; Max-Age=0; Path=/; Secure; SameSite=Lax"
            for cookie in cookies
        )


class ForwardedHeadersTrustTest(unittest.TestCase):
    @staticmethod
    def test_untrusted_client_ip_with_forwarded_headers_is_rejected() -> None:
        headers = Message()
        headers["x-forwarded-email"] = "operator@unit.test"
        headers["x-forwarded-groups"] = base64.b64encode(b'["operators"]').decode()
        headers["x-forwarded-preferred-username"] = "operator"
        headers["x-forwarded-user"] = "dex-subject"

        handler = object.__new__(sso.Handler)
        handler.command = "GET"
        handler.path = "/api/v1/audits"
        handler.headers = headers
        handler.client_address = ("198.51.100.23", 45678)
        handler.proxy = mock.Mock()
        handler.send_error = mock.Mock()

        with mock.patch.dict(
            sso.os.environ,
            {
                "CELL_ID": "ctrl-eaws-lh1",
                "JWT_KEY": "jwt-key",
                "OIDC_ISSUER": "https://dex.unit.test",
                "OPERATOR_GROUP": "operators",
            },
        ):
            handler.handle_request()
        handler.send_error.assert_called_once_with(HTTPStatus.UNAUTHORIZED)
        handler.proxy.assert_not_called()

    @staticmethod
    @mock.patch.object(sso, "resolve_user", return_value=USER_ID_7)
    def test_trusted_proxy_ip_allows_forwarded_headers(mock_resolve_user: mock.Mock) -> None:
        del mock_resolve_user
        headers = Message()
        headers["x-forwarded-email"] = "operator@unit.test"
        headers["x-forwarded-groups"] = base64.b64encode(b'["operators"]').decode()
        headers["x-forwarded-preferred-username"] = "operator"
        headers["x-forwarded-user"] = "dex-subject"

        handler = object.__new__(sso.Handler)
        handler.command = "GET"
        handler.path = "/api/v1/audits"
        handler.headers = headers
        handler.client_address = ("10.244.0.15", 45678)
        handler.proxy = mock.Mock()
        handler.send_error = mock.Mock()

        with mock.patch.dict(
            sso.os.environ,
            {
                "CELL_ID": "ctrl-eaws-lh1",
                "JWT_KEY": "jwt-key",
                "OIDC_ISSUER": "https://dex.unit.test",
                "OPERATOR_GROUP": "operators",
                "TRUSTED_PROXIES": "10.244.0.0/16",
            },
        ):
            handler.handle_request()
        handler.send_error.assert_not_called()
        handler.proxy.assert_called_once()

    @staticmethod
    @mock.patch.object(sso, "resolve_user", return_value=USER_ID_7)
    def test_cryptographically_verified_forwarded_headers_allowed_from_any_ip(
        mock_resolve_user: mock.Mock,
    ) -> None:
        del mock_resolve_user
        headers = Message()
        headers["x-forwarded-email"] = "operator@unit.test"
        headers["x-forwarded-groups"] = base64.b64encode(b'["operators"]').decode()
        headers["x-forwarded-preferred-username"] = "operator"
        headers["x-forwarded-user"] = "dex-subject"
        data = ":".join(headers.get(name, "") for name in sorted(sso.FORWARDED_IDENTITY_HEADERS))
        headers["x-forwarded-signature"] = hmac.new(
            b"jwt-key", data.encode(), hashlib.sha256
        ).hexdigest()

        handler = object.__new__(sso.Handler)
        handler.command = "GET"
        handler.path = "/api/v1/audits"
        handler.headers = headers
        handler.client_address = ("198.51.100.23", 45678)
        handler.proxy = mock.Mock()
        handler.send_error = mock.Mock()

        with mock.patch.dict(
            sso.os.environ,
            {
                "CELL_ID": "ctrl-eaws-lh1",
                "JWT_KEY": "jwt-key",
                "OIDC_ISSUER": "https://dex.unit.test",
                "OPERATOR_GROUP": "operators",
            },
        ):
            handler.handle_request()
        handler.send_error.assert_not_called()
        handler.proxy.assert_called_once()

    @staticmethod
    def test_static_forwarded_verification_header_rejected() -> None:
        headers = Message()
        headers["x-forwarded-email"] = "operator@unit.test"
        headers["x-forwarded-groups"] = base64.b64encode(b'["operators"]').decode()
        headers["x-forwarded-preferred-username"] = "operator"
        headers["x-forwarded-user"] = "dex-subject"
        headers["x-forwarded-verification"] = hmac.new(
            b"jwt-key", b"dragonfly-sso-proxy", hashlib.sha256
        ).hexdigest()

        handler = object.__new__(sso.Handler)
        handler.command = "GET"
        handler.path = "/api/v1/audits"
        handler.headers = headers
        handler.client_address = ("198.51.100.23", 45678)
        handler.proxy = mock.Mock()
        handler.send_error = mock.Mock()

        with mock.patch.dict(
            sso.os.environ,
            {
                "CELL_ID": "ctrl-eaws-lh1",
                "JWT_KEY": "jwt-key",
                "OIDC_ISSUER": "https://dex.unit.test",
                "OPERATOR_GROUP": "operators",
            },
        ):
            handler.handle_request()
        handler.send_error.assert_called_once_with(HTTPStatus.UNAUTHORIZED)
        handler.proxy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
