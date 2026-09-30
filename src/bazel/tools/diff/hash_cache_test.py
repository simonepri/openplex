"""Unit tests for BuildBuddy remote hash cache transport."""

from __future__ import annotations

import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.bazel.tools.diff.hash_cache import (
    decode_proto,
    encode_field,
    encode_varint,
    fetch,
    post_grpc,
    resolve_buildbuddy_api_key,
    store,
)


class TestHashCacheAuth(unittest.TestCase):
    """Test resolution of BuildBuddy credentials."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_env_var_priority(self) -> None:
        with patch.dict("os.environ", {"BUILDBUDDY_API_KEY": "env-key"}):
            self.assertEqual(resolve_buildbuddy_api_key(self.repo), "env-key")

    def test_bazelrc_fallback(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("pathlib.Path.home") as mock_home:
            mock_home.return_value = self.repo
            bazelrc = self.repo / ".bazelrc"
            bazelrc.write_text(
                "build --remote_header=x-buildbuddy-api-key=rc-key\n", encoding="utf-8"
            )
            self.assertEqual(resolve_buildbuddy_api_key(self.repo), "rc-key")

    def test_credential_helper_fallback(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pathlib.Path.home", return_value=self.repo),
            patch("os.access", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            helper = self.repo / "src/bazel/tools/buildbuddy/credential_helper.sh"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.touch()
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"headers": {"x-buildbuddy-api-key": ["helper-key"]}}),
            )
            self.assertEqual(resolve_buildbuddy_api_key(self.repo), "helper-key")

    def test_git_config_fallback(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pathlib.Path.home", return_value=self.repo),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="git-key\n")
            self.assertEqual(resolve_buildbuddy_api_key(self.repo), "git-key")


class TestHashCacheEncoding(unittest.TestCase):
    """Test protobuf encoding and decoding helpers."""

    def test_varint_roundtrip(self) -> None:
        for val in [0, 1, 127, 128, 300, 16384]:
            enc = encode_varint(val)
            fields = decode_proto(encode_field(1, val))
            self.assertEqual(fields[1][0], val)
            self.assertTrue(len(enc) > 0)

    def test_field_types(self) -> None:
        encoded = encode_field(1, "hello") + encode_field(2, 42) + encode_field(3, b"\x01\x02\x03")
        decoded = decode_proto(encoded)
        self.assertEqual(decoded[1][0], b"hello")
        self.assertEqual(decoded[2][0], 42)
        self.assertEqual(decoded[3][0], b"\x01\x02\x03")


class TestHashCacheClient(unittest.TestCase):
    """Test remote cache fetch and store behaviors."""

    @patch("subprocess.run")
    def test_post_grpc_success(self, mock_run: MagicMock) -> None:
        header = b"HTTP/2 200\r\ngrpc-status: 0\r\n\r\n"
        body = b"\x00\x00\x00\x00\x02ok"
        mock_run.return_value = MagicMock(returncode=0, stdout=header + body)
        status, res_body = post_grpc("GetActionResult", b"payload", "key")
        self.assertEqual(status, 0)
        self.assertEqual(res_body, body)

    @patch("src.bazel.tools.diff.hash_cache.resolve_buildbuddy_api_key", return_value=None)
    def test_fetch_no_api_key(self, _mock_key: MagicMock) -> None:
        self.assertIsNone(fetch("test-key"))

    @patch("src.bazel.tools.diff.hash_cache.post_grpc")
    @patch("src.bazel.tools.diff.hash_cache.resolve_buildbuddy_api_key", return_value="test-key")
    def test_fetch_action_cache_miss(self, _mock_key: MagicMock, mock_post: MagicMock) -> None:
        mock_post.return_value = (5, b"")
        self.assertIsNone(fetch("test-key"))

    @patch("subprocess.run")
    @patch("src.bazel.tools.diff.hash_cache.post_grpc")
    @patch("src.bazel.tools.diff.hash_cache.resolve_buildbuddy_api_key", return_value="test-key")
    def test_fetch_success(
        self, _mock_key: MagicMock, mock_post: MagicMock, mock_run: MagicMock
    ) -> None:
        digest_pb = encode_field(1, "cas-hash") + encode_field(2, 4)
        file_pb = encode_field(1, "hashes.json") + encode_field(2, digest_pb)
        action_res_pb = encode_field(1, file_pb)
        framed = b"\x00" + struct.pack(">I", len(action_res_pb)) + action_res_pb
        mock_post.return_value = (0, framed)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"data")

        res = fetch("test-key")
        self.assertEqual(res, b"data")
        mock_run.assert_called_once_with(
            ["bb", "download", "cas-hash/4", "--api_key=test-key"], capture_output=True, check=False
        )

    @patch("subprocess.run")
    @patch("src.bazel.tools.diff.hash_cache.post_grpc")
    @patch("src.bazel.tools.diff.hash_cache.resolve_buildbuddy_api_key", return_value="test-key")
    def test_fetch_cas_error_logs_warn(
        self, _mock_key: MagicMock, mock_post: MagicMock, mock_run: MagicMock
    ) -> None:
        digest_pb = encode_field(1, "cas-hash") + encode_field(2, 4)
        file_pb = encode_field(1, "hashes.json") + encode_field(2, digest_pb)
        action_res_pb = encode_field(1, file_pb)
        framed = b"\x00" + struct.pack(">I", len(action_res_pb)) + action_res_pb
        mock_post.return_value = (0, framed)
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")

        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            res = fetch("test-key")
            self.assertIsNone(res)
            self.assertIn("[WARN]", mock_err.getvalue())

    @patch("src.bazel.tools.diff.hash_cache.resolve_buildbuddy_api_key", return_value=None)
    def test_store_no_api_key(self, _mock_key: MagicMock) -> None:
        with patch("subprocess.run") as mock_run:
            store("test-key", b"data")
            mock_run.assert_not_called()

    @patch("subprocess.run")
    @patch("src.bazel.tools.diff.hash_cache.post_grpc")
    @patch("src.bazel.tools.diff.hash_cache.resolve_buildbuddy_api_key", return_value="test-key")
    def test_store_success(
        self, _mock_key: MagicMock, mock_post: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        mock_post.return_value = (0, b"")
        store("test-key", b"data")
        mock_run.assert_called_once()
        mock_post.assert_called_once()

    @patch("subprocess.run")
    @patch("src.bazel.tools.diff.hash_cache.resolve_buildbuddy_api_key", return_value="test-key")
    def test_store_upload_error_logs_warn(self, _mock_key: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            store("test-key", b"data")
            self.assertIn("[WARN]", mock_err.getvalue())


if __name__ == "__main__":
    unittest.main()
