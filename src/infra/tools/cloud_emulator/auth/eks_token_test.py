"""Protects temporary EKS authentication and credential isolation."""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from unittest.mock import patch

from src.infra.tools.cloud_emulator.auth import eks_token


class EksTokenTest(unittest.TestCase):
    @staticmethod
    def test_uses_temporary_session_without_inheriting_operator_credentials() -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def run(
            command: list[str], *, env: dict[str, str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((command, dict(env)))
            if command[1:3] == ["sts", "get-session-token"]:
                payload = {
                    "Credentials": {
                        "AccessKeyId": "ASIA-issued-key",
                        "SecretAccessKey": "issued-secret",
                        "SessionToken": "issued-session",
                    }
                }
            else:
                payload = {
                    "apiVersion": "client.authentication.k8s.io/v1beta1",
                    "kind": "ExecCredential",
                    "status": {
                        "token": "k8s-aws-v1.signed-token",
                        "expirationTimestamp": "2099-01-01T00:00:00Z",
                    },
                }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        output = io.StringIO()
        now = datetime.now(UTC)
        with (
            patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": "operator-key",
                    "AWS_SECRET_ACCESS_KEY": "operator-secret",
                    "AWS_SESSION_TOKEN": "operator-session",
                    "AWS_PROFILE": "production",
                    "AWS_ENDPOINT_URL_STS": "https://sts.amazonaws.com",
                },
            ),
            patch.object(eks_token.subprocess, "run", side_effect=run),
            redirect_stdout(output),
        ):
            code = eks_token.main([
                "--cluster-name",
                "cell-eaws-lh1",
                "--endpoint-url",
                "http://127.0.0.1:4566",
                "--region",
                "us-west-2",
            ])

        assert code == 0
        credential = json.loads(output.getvalue())
        assert credential["status"]["token"] == "k8s-aws-v1.signed-token"
        lifetime = datetime.fromisoformat(credential["status"]["expirationTimestamp"]) - now
        assert lifetime.total_seconds() > 600
        assert lifetime.total_seconds() < 900
        assert len(calls) == 2
        assert calls[0][1]["AWS_ACCESS_KEY_ID"] == "test"
        assert calls[0][1]["AWS_SECRET_ACCESS_KEY"] == "test"
        assert "AWS_SESSION_TOKEN" not in calls[0][1]
        assert calls[1][1]["AWS_ACCESS_KEY_ID"] == "ASIA-issued-key"
        assert calls[1][1]["AWS_SECRET_ACCESS_KEY"] == "issued-secret"
        assert calls[1][1]["AWS_SESSION_TOKEN"] == "issued-session"
        assert "cell-eaws-lh1" in calls[1][0]
        for command, environment in calls:
            assert "http://127.0.0.1:4566" in command
            assert "us-west-2" in command
            assert environment["AWS_CONFIG_FILE"] == os.devnull
            assert "AWS_PROFILE" not in environment
            assert "AWS_ENDPOINT_URL_STS" not in environment
            assert "issued-secret" not in command
            assert "issued-session" not in command
        assert "issued-secret" not in output.getvalue()
        assert "issued-session" not in output.getvalue()

    def test_rejects_incomplete_session_without_signing_or_exposing_credentials(self) -> None:
        for missing in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
            with self.subTest(missing=missing):
                credentials = dict.fromkeys(
                    ("AccessKeyId", "SecretAccessKey", "SessionToken"), "private-credential"
                )
                del credentials[missing]
                completed = subprocess.CompletedProcess(
                    [], 0, json.dumps({"Credentials": credentials}), ""
                )
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.object(eks_token.subprocess, "run", return_value=completed) as run,
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    code = eks_token.main([
                        "--cluster-name",
                        "cell-eaws-lh1",
                        "--endpoint-url",
                        "http://127.0.0.1:4566",
                        "--region",
                        "us-west-2",
                    ])

                assert code == 1
                assert run.call_count == 1
                assert output.getvalue() == ""
                assert "private-credential" not in errors.getvalue()

    @staticmethod
    def test_cli_failure_does_not_print_captured_credentials() -> None:
        failure = subprocess.CalledProcessError(
            1, ["aws"], output="private-credential", stderr="private-credential"
        )
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(eks_token.subprocess, "run", side_effect=failure),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = eks_token.main([
                "--cluster-name",
                "cell-eaws-lh1",
                "--endpoint-url",
                "http://127.0.0.1:4566",
                "--region",
                "us-west-2",
            ])

        assert code == 1
        assert output.getvalue() == ""
        assert "private-credential" not in errors.getvalue()

    def test_rejects_remote_endpoint_before_requesting_credentials(self) -> None:
        errors = io.StringIO()
        with (
            patch.object(eks_token.subprocess, "run") as run,
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as exited,
        ):
            eks_token.main([
                "--cluster-name",
                "cell-eaws-lh1",
                "--endpoint-url",
                "https://sts.amazonaws.com",
                "--region",
                "us-west-2",
            ])

        assert exited.exception.code == 2
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
