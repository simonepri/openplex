#!/usr/bin/env python3
"""Issues Kubernetes exec credentials using a temporary Floci STS session."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

UTC = timezone.utc  # ruff: ignore[datetime-timezone-utc]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--endpoint-url", required=True, type=loopback_endpoint)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    try:
        credential = issue_credential(args.cluster_name, args.endpoint_url, args.region)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Unable to issue Floci EKS credentials.", file=sys.stderr)
        return 1
    print(json.dumps(credential))
    return 0


def issue_credential(cluster_name: str, endpoint: str, region: str) -> dict[str, Any]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("AWS_")}
    environment.update({
        # keep-sorted start
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_CLI_AUTO_PROMPT": "off",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_PAGER": "",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        # keep-sorted end
    })
    session = aws_json(
        ["sts", "get-session-token", "--duration-seconds", "900"], endpoint, region, environment
    )
    credentials = session.get("Credentials")
    if not isinstance(credentials, dict):
        raise ValueError("STS credentials are missing")
    for variable, field in (
        # keep-sorted start
        ("AWS_ACCESS_KEY_ID", "AccessKeyId"),
        ("AWS_SECRET_ACCESS_KEY", "SecretAccessKey"),
        ("AWS_SESSION_TOKEN", "SessionToken"),
        # keep-sorted end
    ):
        value = credentials.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError("STS credentials are incomplete")
        environment[variable] = value

    # Floci authenticator accepts standard token lifetime (#4186); refresh before STS session expires.
    expiration = datetime.now(UTC) + timedelta(minutes=14)
    credential = aws_json(
        ["eks", "get-token", "--cluster-name", cluster_name], endpoint, region, environment
    )
    status = credential.get("status")
    if (
        credential.get("kind") != "ExecCredential"
        or credential.get("apiVersion")
        not in {"client.authentication.k8s.io/v1", "client.authentication.k8s.io/v1beta1"}
        or not isinstance(status, dict)
        or not isinstance(status.get("token"), str)
        or not status["token"]
    ):
        raise ValueError("EKS exec credential is incomplete")
    return {
        "apiVersion": credential["apiVersion"],
        "kind": "ExecCredential",
        "status": {
            "expirationTimestamp": expiration.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "token": status["token"],
        },
    }


def aws_json(
    arguments: list[str], endpoint: str, region: str, environment: dict[str, str]
) -> dict[str, Any]:
    completed = subprocess.run(
        ["aws", *arguments, "--endpoint-url", endpoint, "--region", region, "--output", "json"],
        env=environment,
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise ValueError("AWS CLI response is not an object")
    return payload


def loopback_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("Floci endpoint must be an HTTP loopback URL")
    return endpoint


if __name__ == "__main__":
    sys.exit(main())
