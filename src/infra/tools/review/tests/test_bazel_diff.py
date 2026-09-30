"""Tests Bazel dependency diff analysis, impacted target resolution, and reverse dependency extraction logic."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.infra.tools.review.context import bazel_diff


class TestBazelDiff(unittest.TestCase):
    @patch("subprocess.run")
    def test_run_bazel_query_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="//src/foo:bar\n//src/foo:bar_test\n",
        )
        res = bazel_diff.run_bazel_query(Path("/repo"), "set(//src/foo:bar)")
        self.assertEqual(res, ["//src/foo:bar", "//src/foo:bar_test"])

    @patch("subprocess.run")
    def test_run_bazel_query_keep_going_code_3(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=3,
            stdout="ERROR: Skipping target\n//src/foo:valid_target\n",
            stderr="",
        )
        res = bazel_diff.run_bazel_query(Path("/repo"), "set(...)")
        self.assertEqual(res, ["//src/foo:valid_target"])

    @patch("subprocess.run")
    def test_run_bazel_query_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=7, stdout="", stderr="ERROR: bad")
        with self.assertRaises(bazel_diff.BazelQueryError):
            bazel_diff.run_bazel_query(Path("/repo"), "set(...)")

    @patch("src.infra.tools.review.context.bazel_diff.run_bazel_query")
    def test_get_bazel_diff_impact(self, mock_query: MagicMock) -> None:
        mock_query.return_value = [
            "source file //src/foo:bar.py",
            "py_test rule //src/foo:bar_test",
            "go_binary rule //src/app:bin",
        ]
        with patch.object(Path, "exists", return_value=True):
            res = bazel_diff.get_bazel_diff(Path("/repo"), ["src/foo/bar.py"])
            self.assertEqual(res.direct_targets, ["//src/foo:bar.py"])
            self.assertEqual(res.affected_tests, ["//src/foo:bar_test"])
            self.assertIn("//src/app:bin", res.affected_targets)


if __name__ == "__main__":
    unittest.main()
