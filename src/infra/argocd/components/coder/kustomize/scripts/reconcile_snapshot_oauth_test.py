"""Defend repeatable OAuth registration, credential persistence, and portal rollout ordering."""

from __future__ import annotations

import base64
import copy
import unittest
from typing import override
from unittest.mock import patch

from bootstrap_template_publisher import BootstrapError, CoderAPI
from reconcile_snapshot_oauth import (
    APP_NAME,
    APPS_PATH,
    DEPLOYMENT_PATH,
    EXTERNAL_PATH,
    SOURCE_PATH,
    TARGET_PATH,
    KubernetesAPI,
    decode_secret,
    reconcile,
    validate_runtime_record,
)

APP_ID = "00000000-0000-4000-8000-000000000001"
SECRET_ID = "00000000-0000-4000-8000-000000000002"
CALLBACK_URL = "https://snapshots.example.com/oauth/callback"


class FakeCoder(CoderAPI):
    def __init__(self) -> None:
        self.apps: list[dict[str, object]] = []
        self.secrets: list[dict[str, object]] = []
        self.calls: list[tuple[str, str]] = []

    @override
    def request(
        self, method: str, path: str, *, payload: object | None = None, accepted: set[int]
    ) -> tuple[int, object | None]:
        del accepted
        self.calls.append((method, path))
        if path == APPS_PATH and method == "GET":
            return 200, copy.deepcopy(self.apps)
        if path == APPS_PATH and method == "POST":
            assert isinstance(payload, dict)
            self.apps.append({"id": APP_ID, **payload})
            return 201, copy.deepcopy(self.apps[-1])
        if path == f"{APPS_PATH}/{APP_ID}" and method == "PUT":
            assert isinstance(payload, dict)
            self.apps[0].update(payload)
            return 200, copy.deepcopy(self.apps[0])
        if path == f"{APPS_PATH}/{APP_ID}/secrets" and method == "GET":
            return 200, copy.deepcopy(self.secrets)
        if path == f"{APPS_PATH}/{APP_ID}/secrets" and method == "POST":
            self.secrets.append({"id": SECRET_ID})
            return 201, {"id": SECRET_ID, "client_secret_full": "generated-test-value"}
        raise AssertionError((method, path))


class FakeKubernetes(KubernetesAPI):
    def __init__(self) -> None:
        self.source: dict[str, object] = {"metadata": {"resourceVersion": "1"}}
        self.portal_exists = False
        self.synchronizes = True
        self.fail_write = False
        self.calls: list[tuple[str, str]] = []
        self.version = ""

    @override
    def request(
        self, method: str, path: str, *, payload: object | None = None, accepted: set[int]
    ) -> tuple[int, object | None]:
        del accepted
        self.calls.append((method, path))
        if path == SOURCE_PATH and method == "GET":
            return 200, copy.deepcopy(self.source)
        if path == SOURCE_PATH and method == "PATCH":
            if self.fail_write:
                raise BootstrapError("runtime write failed")
            assert isinstance(payload, dict)
            self.source.update(payload)
            return 200, copy.deepcopy(self.source)
        if path == EXTERNAL_PATH:
            return (200, {}) if self.portal_exists else (404, None)
        if path == TARGET_PATH and method == "GET":
            data = decode_secret(self.source)
            expected = {
                "CODER_OAUTH_CLIENT_ID": data["client_id"],
                "CODER_OAUTH_CLIENT_SECRET": data["client_secret"],
                "SESSION_SECRET": data["session_secret"],
            }
            return 200, {
                "data": {
                    key: base64.b64encode(value.encode()).decode()
                    for key, value in expected.items()
                }
            } if self.synchronizes else {}
        if path == DEPLOYMENT_PATH and method == "GET":
            return 200, {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "coder.openplex.dev/oauth-version": self.version,
                            }
                        }
                    }
                }
            }
        if path == DEPLOYMENT_PATH and method == "PATCH":
            assert isinstance(payload, dict)
            self.version = payload["spec"]["template"]["metadata"]["annotations"][
                "coder.openplex.dev/oauth-version"
            ]
            return 200, {}
        raise AssertionError((method, path))


class SnapshotOAuthTest(unittest.TestCase):
    def test_first_run_registers_and_persists_before_portal_creation(self) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        reconcile(coder, kube, CALLBACK_URL)
        data = decode_secret(kube.source)
        assert coder.apps == [
            {"id": APP_ID, "name": APP_NAME, "callback_url": CALLBACK_URL, "icon": ""}
        ]
        assert data["client_id"] == APP_ID
        assert data["client_secret_id"] == SECRET_ID
        assert data["client_secret"] == "generated-test-value"
        assert len(data["session_secret"]) >= 32
        assert ("PATCH", SOURCE_PATH) in kube.calls
        assert ("PATCH", DEPLOYMENT_PATH) not in kube.calls

    def test_repeat_run_reuses_registration_secret_and_session_key(self) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        reconcile(coder, kube, CALLBACK_URL)
        original = copy.deepcopy(kube.source)
        coder.calls.clear()
        kube.calls.clear()
        reconcile(coder, kube, CALLBACK_URL)
        assert kube.source == original
        assert all(method == "GET" for method, _ in coder.calls)
        assert ("PATCH", SOURCE_PATH) not in kube.calls

    def test_callback_drift_updates_existing_app_without_rotating_credentials(self) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        reconcile(coder, kube, CALLBACK_URL)
        original = copy.deepcopy(kube.source)
        changed = "https://new.example.com/oauth/callback"
        reconcile(coder, kube, changed)
        assert coder.apps[0]["callback_url"] == changed
        assert kube.source == original
        assert len(coder.secrets) == 1

    def test_deleted_provider_secret_is_replaced_without_changing_session_key(self) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        reconcile(coder, kube, CALLBACK_URL)
        session = decode_secret(kube.source)["session_secret"]
        coder.secrets.clear()
        coder.calls.clear()
        reconcile(coder, kube, CALLBACK_URL)
        assert ("POST", f"{APPS_PATH}/{APP_ID}/secrets") in coder.calls
        assert decode_secret(kube.source)["session_secret"] == session

    def test_wrong_application_id_and_duplicate_names_fail_before_credential_writes(self) -> None:
        for wrong_name in (False, True):
            with self.subTest(wrong_name=wrong_name):
                coder, kube = FakeCoder(), FakeKubernetes()
                reconcile(coder, kube, CALLBACK_URL)
                if wrong_name:
                    coder.apps[0]["name"] = "different-application"
                else:
                    coder.apps.append(copy.deepcopy(coder.apps[0]))
                kube.calls.clear()
                with self.assertRaises(BootstrapError):
                    reconcile(coder, kube, CALLBACK_URL)
                assert ("PATCH", SOURCE_PATH) not in kube.calls

    def test_failed_record_write_preserves_previous_credentials_and_does_not_roll_portal(
        self,
    ) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        kube.fail_write = True
        original = copy.deepcopy(kube.source)
        with self.assertRaises(BootstrapError):
            reconcile(coder, kube, CALLBACK_URL)
        assert kube.source == original
        assert ("GET", EXTERNAL_PATH) not in kube.calls

    def test_existing_portal_rolls_once_after_target_credentials_match(self) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        kube.portal_exists = True
        reconcile(coder, kube, CALLBACK_URL)
        assert kube.calls.index(("PATCH", SOURCE_PATH)) < kube.calls.index(("PATCH", EXTERNAL_PATH))
        assert kube.calls.index(("GET", TARGET_PATH)) < kube.calls.index(("PATCH", DEPLOYMENT_PATH))
        assert kube.version
        kube.calls.clear()
        reconcile(coder, kube, CALLBACK_URL)
        assert ("PATCH", DEPLOYMENT_PATH) not in kube.calls

    def test_unsynchronized_external_secret_fails_without_rolling_portal(self) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        kube.portal_exists = True
        kube.synchronizes = False
        with patch("reconcile_snapshot_oauth.time.monotonic", side_effect=[0, 121]):
            with self.assertRaises(BootstrapError):
                reconcile(coder, kube, CALLBACK_URL)
        assert ("PATCH", DEPLOYMENT_PATH) not in kube.calls

    def test_injected_publisher_requires_complete_provisioned_oauth_record(self) -> None:
        kube = FakeKubernetes()
        with patch("reconcile_snapshot_oauth.KubernetesAPI", return_value=kube):
            with self.assertRaises(BootstrapError):
                validate_runtime_record()
        assert ("GET", EXTERNAL_PATH) not in kube.calls

    def test_injected_publisher_preserves_provisioned_oauth_record(self) -> None:
        coder, kube = FakeCoder(), FakeKubernetes()
        reconcile(coder, kube, CALLBACK_URL)
        original = copy.deepcopy(kube.source)
        with patch("reconcile_snapshot_oauth.KubernetesAPI", return_value=kube):
            validate_runtime_record()
        assert kube.source == original

    def test_invalid_callback_fails_before_any_authenticated_request(self) -> None:
        for callback in (
            "http://snapshots.example.invalid/oauth/callback",
            "https://coder-snapshots.example.invalid/oauth/callback",
            CALLBACK_URL + "?redirect=evil",
            "https://user:password@example.invalid/oauth/callback",
        ):
            with self.subTest(callback=callback):
                coder, kube = FakeCoder(), FakeKubernetes()
                with self.assertRaises(BootstrapError):
                    reconcile(coder, kube, callback)
                assert coder.calls == []
                assert kube.calls == []


if __name__ == "__main__":
    unittest.main()
