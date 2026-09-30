"""Reconcile the snapshot portal's Coder registration and private runtime record."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

from coder_api import BootstrapError, RejectAPIRedirects

if TYPE_CHECKING:
    from coder_api import CoderAPI

APP_NAME = "workspace-snapshot-portal"
APPS_PATH = "/api/v2/oauth2-provider/apps"
SOURCE_PATH = "/api/v1/namespaces/application-identity/secrets/coder-snapshot-oauth"
PORTAL_NAMESPACE = "coder-workspace-backup-system"
PORTAL_NAME = "coder-snapshot-portal"
TARGET_PATH = f"/api/v1/namespaces/{PORTAL_NAMESPACE}/secrets/{PORTAL_NAME}-secrets"
EXTERNAL_PATH = (
    f"/apis/external-secrets.io/v1/namespaces/{PORTAL_NAMESPACE}/externalsecrets/"
    f"{PORTAL_NAME}-secrets"
)
DEPLOYMENT_PATH = f"/apis/apps/v1/namespaces/{PORTAL_NAMESPACE}/deployments/{PORTAL_NAME}"
TOKEN_DIRECTORY = Path("/var/run/coder/oauth-bootstrap")


def object_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BootstrapError("OAuth reconciliation received an invalid object")
    return cast("dict[str, object]", value)


def string_value(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BootstrapError("OAuth reconciliation received an empty or invalid field")
    return value


def identifier(value: object) -> str:
    try:
        return str(uuid.UUID(string_value(value)))
    except ValueError as error:
        raise BootstrapError("Coder returned an invalid OAuth identifier") from error


class KubernetesAPI:
    """Access named bootstrap resources using the projected, short-lived identity."""

    def __init__(self) -> None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT_HTTPS"]
        self.url = f"https://{host}:{port}"
        self.token = (TOKEN_DIRECTORY / "token").read_text().strip()
        context = ssl.create_default_context(cafile=str(TOKEN_DIRECTORY / "ca.crt"))
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), RejectAPIRedirects()
        )

    def request(
        self, method: str, path: str, *, payload: object | None = None, accepted: set[int]
    ) -> tuple[int, object | None]:
        request = urllib.request.Request(
            self.url + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/merge-patch+json",
            },
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                status = response.status
                value: object = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as error:
            if error.code in accepted:
                return error.code, None
            raise BootstrapError(f"OAuth runtime record API returned HTTP {error.code}") from None
        except (urllib.error.URLError, ValueError) as error:
            raise BootstrapError("OAuth runtime record API was unreachable or invalid") from error
        assert isinstance(status, int)
        if status not in accepted:
            raise BootstrapError(f"OAuth runtime record API returned HTTP {status}")
        return status, value


def decode_secret(value: object) -> dict[str, str]:
    record = object_value(value)
    encoded = object_value(record.get("data", {}))
    try:
        return {
            key: base64.b64decode(string_value(content), validate=True).decode()
            for key, content in encoded.items()
        }
    except (binascii.Error, UnicodeDecodeError) as error:
        raise BootstrapError("OAuth runtime record has invalid encoding") from error


def validate_callback(callback_url: str) -> None:
    callback = urllib.parse.urlsplit(callback_url)
    if (
        callback.scheme != "https"
        or not callback.hostname
        or callback.hostname.rstrip(".").endswith(".invalid")
        or callback.username
        or callback.password
        or callback.query
        or callback.fragment
        or callback.path != "/oauth/callback"
    ):
        raise BootstrapError("Snapshot OAuth requires an exact HTTPS /oauth/callback URL")


def reconcile(api: CoderAPI, kube: KubernetesAPI, callback_url: str) -> None:
    validate_callback(callback_url)
    _, source_value = kube.request("GET", SOURCE_PATH, accepted={200})
    source = object_value(source_value)
    current = decode_secret(source)
    app = reconcile_app(api, callback_url, current.get("client_id", ""))
    app_id = identifier(app.get("id"))
    values = reconcile_secret(api, app_id, current)
    if values != current:
        resource_version = string_value(object_value(source.get("metadata")).get("resourceVersion"))
        kube.request(
            "PATCH",
            SOURCE_PATH,
            payload={
                "metadata": {"resourceVersion": resource_version},
                "data": {
                    key: base64.b64encode(value.encode()).decode() for key, value in values.items()
                },
            },
            accepted={200},
        )
    refresh_portal(kube, values)


def reconcile_app(api: CoderAPI, callback_url: str, recorded_id: str) -> dict[str, object]:
    _, response = api.request("GET", APPS_PATH, accepted={200})
    if not isinstance(response, list):
        raise BootstrapError("Coder returned an invalid OAuth application inventory")
    apps = [object_value(app) for app in response]
    if recorded_id and any(
        app.get("id") == recorded_id and app.get("name") != APP_NAME for app in apps
    ):
        raise BootstrapError("Recorded snapshot OAuth ID belongs to a different application")
    matching = [app for app in apps if app.get("name") == APP_NAME]
    if len(matching) > 1:
        raise BootstrapError("Coder has duplicate snapshot OAuth applications")
    payload = {"name": APP_NAME, "callback_url": callback_url, "icon": ""}
    if not matching:
        _, value = api.request("POST", APPS_PATH, payload=payload, accepted={201})
        app = object_value(value)
    else:
        app = matching[0]
        if app.get("callback_url") != callback_url:
            app_id = identifier(app.get("id"))
            _, value = api.request("PUT", f"{APPS_PATH}/{app_id}", payload=payload, accepted={200})
            app = object_value(value)
    if app.get("name") != APP_NAME or app.get("callback_url") != callback_url:
        raise BootstrapError("Coder returned a mismatched snapshot OAuth application")
    return app


def reconcile_secret(api: CoderAPI, app_id: str, current: dict[str, str]) -> dict[str, str]:
    path = f"{APPS_PATH}/{app_id}/secrets"
    _, response = api.request("GET", path, accepted={200})
    if not isinstance(response, list):
        raise BootstrapError("Coder returned an invalid OAuth secret inventory")
    ids = {identifier(object_value(value).get("id")) for value in response}
    values = dict(current)
    if (
        values.get("client_id") != app_id
        or values.get("client_secret_id") not in ids
        or not values.get("client_secret")
    ):
        _, response = api.request("POST", path, accepted={201})
        created = object_value(response)
        values.update({
            "client_id": app_id,
            "client_secret_id": identifier(created.get("id")),
            "client_secret": string_value(created.get("client_secret_full")),
        })
    if not values.get("session_secret"):
        values["session_secret"] = secrets.token_urlsafe(32)
    return values


def refresh_portal(kube: KubernetesAPI, values: dict[str, str]) -> None:
    status, _ = kube.request("GET", EXTERNAL_PATH, accepted={200, 404})
    if status == 404:
        return  # The portal's later bootstrap wave consumes the new record on first creation.
    expected = {
        "CODER_OAUTH_CLIENT_ID": values["client_id"],
        "CODER_OAUTH_CLIENT_SECRET": values["client_secret"],
        "SESSION_SECRET": values["session_secret"],
    }
    version = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    kube.request(
        "PATCH",
        EXTERNAL_PATH,
        payload={"metadata": {"annotations": {"force-sync": str(time.time_ns())}}},
        accepted={200},
    )
    deadline = time.monotonic() + 120
    while True:
        status, target = kube.request("GET", TARGET_PATH, accepted={200, 404})
        if status == 200 and decode_secret(target) == expected:
            break
        if time.monotonic() >= deadline:
            raise BootstrapError(
                "Snapshot portal credentials did not synchronize within 120 seconds"
            )
        time.sleep(2)
    status, deployment = kube.request("GET", DEPLOYMENT_PATH, accepted={200, 404})
    if status == 404:
        return
    spec = object_value(object_value(deployment).get("spec"))
    template = object_value(spec.get("template"))
    annotations = object_value(object_value(template.get("metadata", {})).get("annotations", {}))
    if annotations.get("coder.openplex.dev/oauth-version") != version:
        kube.request(
            "PATCH",
            DEPLOYMENT_PATH,
            payload={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "coder.openplex.dev/oauth-version": version,
                            }
                        }
                    }
                }
            },
            accepted={200},
        )


def validate_runtime_record() -> None:
    """Require externally provisioned OAuth credentials before staging a publisher token."""
    kube = KubernetesAPI()
    _, source = kube.request("GET", SOURCE_PATH, accepted={200})
    values = decode_secret(source)
    try:
        identifier(values.get("client_id"))
        identifier(values.get("client_secret_id"))
        string_value(values.get("client_secret"))
        if len(string_value(values.get("session_secret"))) < 32:
            raise BootstrapError("Snapshot session key is too short")
    except BootstrapError:
        raise BootstrapError(
            "Provision the coder-snapshot-oauth runtime record with a registered Coder OAuth client "
            "before supplying an external publisher token"
        ) from None
    refresh_portal(kube, values)


if __name__ == "__main__":
    try:
        validate_runtime_record()
    except (BootstrapError, KeyError, OSError) as error:
        message = error.args[0] if error.args else "record validation failed"
        raise SystemExit(f"Coder snapshot OAuth bootstrap failed: {message}") from None
