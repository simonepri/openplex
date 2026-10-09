"""Unit tests for Coder automation token minting and secret-records synchronization."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from infra.tools.cloud_emulator.auth import coder_token


class CoderTokenTest(unittest.TestCase):
    def test_ensure_secret_record_patches_existing_secret(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            coder_token.ensure_secret_record("ctrl-test", "test-token-12345")

            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertIn("kubectl", cmd)
            self.assertIn("--context", cmd)
            self.assertIn("ctrl-test", cmd)
            self.assertIn("secret-records", cmd)
            self.assertIn("patch", cmd)
            self.assertIn("coder-automation-token", cmd)

    def test_ensure_secret_record_creates_if_patch_fails(self) -> None:
        with (
            patch("subprocess.run") as mock_run,
            patch(
                "subprocess.check_output", return_value="apiVersion: v1\nkind: Secret\n"
            ) as mock_output,
        ):
            # First patch call fails
            mock_run.side_effect = [
                MagicMock(returncode=1, stderr="Error from server (NotFound): secrets not found"),
                MagicMock(returncode=0),
            ]
            coder_token.ensure_secret_record("ctrl-test", "test-token-12345")

            mock_output.assert_called_once()
            create_cmd = mock_output.call_args[0][0]
            self.assertIn("create", create_cmd)
            self.assertIn("coder-automation-token", create_cmd)

            self.assertEqual(mock_run.call_count, 2)
            apply_cmd = mock_run.call_args_list[1][0][0]
            self.assertIn("apply", apply_cmd)

    def test_reconcile_with_explicit_token(self) -> None:
        with (
            patch.object(coder_token.compose, "load_local_deployment", return_value={}),
            patch.object(
                coder_token.readiness, "control_cluster_record", return_value="ctrl-eaws-lh1"
            ),
            patch.object(coder_token, "ensure_secret_record") as mock_ensure,
        ):
            result = coder_token.reconcile(Path("/fake/root"), token="explicit-token-abc")
            self.assertEqual(result, "explicit-token-abc")
            mock_ensure.assert_called_once_with("ctrl-eaws-lh1", "explicit-token-abc")

    def test_mint_token_via_api_flow(self) -> None:
        with (
            patch.object(coder_token, "browser_login", return_value="session-token-xyz"),
            patch.object(coder_token, "CoderClient") as mock_client_cls,
        ):
            client = MagicMock()
            mock_client_cls.return_value = client
            # 1. Check existing token -> returns 404
            # 2. POST token -> returns 201 with key
            # 3. Logout -> returns 200
            client.request.side_effect = [
                (404, None),
                (201, {"key": "newly-minted-token-999"}),
                (200, None),
            ]

            token = coder_token.mint_token_via_api(
                "https://coder.corp.local.internal",
                "https://dex.corp.local.internal",
            )
            self.assertEqual(token, "newly-minted-token-999")
            self.assertEqual(client.request.call_count, 3)
            create_call = client.request.call_args_list[1]
            self.assertEqual(create_call[0][0], "POST")
            self.assertEqual(create_call[0][1], "/api/v2/users/me/keys/tokens")
            self.assertEqual(create_call[1]["payload"]["token_name"], "coder-automation-token")


if __name__ == "__main__":
    unittest.main()
