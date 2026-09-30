"""Unit tests verifying bazel-diff output parsing, hash caching, and orchestration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

from src.bazel.tools.diff.impacted import (
    BazelDiffError,
    ImpactedResult,
    ImpactedTarget,
    compute_cache_key,
    get_bazel_diff_version,
    get_impacted_targets,
    get_repo_cache_dir,
    is_working_tree_clean,
    parse_impacted_targets,
    resolve_bazel_diff_cmd,
    resolve_commit_sha,
)


class TestImpactedParsing(unittest.TestCase):
    """Verifies parsing of bazel-diff output formats."""

    def test_parse_empty_output(self) -> None:
        res = parse_impacted_targets("")
        self.assertEqual(res.all_targets, [])
        self.assertEqual(res.direct_targets, [])
        self.assertEqual(res.targets, [])

    def test_parse_json_with_distances(self) -> None:
        fixture = json.dumps([
            {"label": "//src/core:lib", "targetDistance": 0, "packageDistance": 0},
            {"label": "//src/core:lib_test", "targetDistance": 1, "packageDistance": 0},
            {"label": "//src/app:bin", "targetDistance": 2, "packageDistance": 1},
        ])
        res = parse_impacted_targets(fixture)
        self.assertEqual(res.direct_targets, ["//src/core:lib"])
        self.assertEqual(
            res.all_targets,
            ["//src/app:bin", "//src/core:lib", "//src/core:lib_test"],
        )
        self.assertEqual(len(res.targets), 3)
        self.assertEqual(res.targets[0].label, "//src/core:lib")
        self.assertEqual(res.targets[0].target_distance, 0)
        self.assertEqual(res.targets[1].label, "//src/core:lib_test")
        self.assertEqual(res.targets[1].target_distance, 1)

    def test_parse_json_snake_case_fields(self) -> None:
        fixture = json.dumps([
            {"target": "//src/foo:foo", "target_distance": 0, "package_distance": 0},
            {"target": "//src/bar:bar", "target_distance": 3, "package_distance": 2},
        ])
        res = parse_impacted_targets(fixture)
        self.assertEqual(res.direct_targets, ["//src/foo:foo"])
        self.assertEqual(res.all_targets, ["//src/bar:bar", "//src/foo:foo"])

    def test_parse_json_string_list(self) -> None:
        fixture = json.dumps(["//src/a:a", "//src/b:b"])
        res = parse_impacted_targets(fixture)
        self.assertEqual(res.direct_targets, ["//src/a:a", "//src/b:b"])
        self.assertEqual(res.all_targets, ["//src/a:a", "//src/b:b"])

    def test_parse_plain_lines_fallback(self) -> None:
        raw = "//src/a:a\n//src/b:b\n"
        res = parse_impacted_targets(raw)
        self.assertEqual(res.direct_targets, ["//src/a:a", "//src/b:b"])
        self.assertEqual(res.all_targets, ["//src/a:a", "//src/b:b"])

    def test_parse_invalid_json_structure(self) -> None:
        with self.assertRaises(BazelDiffError):
            parse_impacted_targets('{"not": "a list"}')


class TestCacheKey(unittest.TestCase):
    """Verifies cache key invalidation on flags or tool versions."""

    def test_compute_cache_key_invalidation(self) -> None:
        sha1 = "a" * 40
        sha2 = "b" * 40
        key1 = compute_cache_key(sha1, "49.1.0", "standard")
        key2 = compute_cache_key(sha2, "49.1.0", "standard")
        key_ver = compute_cache_key(sha1, "50.0.0", "standard")
        key_flags = compute_cache_key(sha1, "49.1.0", "custom")

        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key_ver)
        self.assertNotEqual(key1, key_flags)


class TestImpactedEngineOrchestration(unittest.TestCase):
    """Verifies commit resolution, worktree lifecycle, and cache lookup order."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("src.bazel.tools.diff.impacted._run_cmd")
    def test_resolve_commit_sha_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = "a" * 40
        sha = resolve_commit_sha(self.repo_root, "HEAD~1")
        self.assertEqual(sha, "a" * 40)
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--verify", "HEAD~1^{commit}"],
            cwd=self.repo_root,
        )

    @patch("src.bazel.tools.diff.impacted._run_cmd")
    def test_resolve_commit_sha_invalid(self, mock_run: MagicMock) -> None:
        mock_run.return_value = "short"
        with self.assertRaises(BazelDiffError):
            resolve_commit_sha(self.repo_root, "invalid")

    def test_get_repo_cache_dir_resolution(self) -> None:
        with mock.patch.dict("os.environ", {"REPO_CACHE_DIR": "/custom/cache"}, clear=True):
            self.assertEqual(get_repo_cache_dir(), Path("/custom/cache"))
        with mock.patch.dict("os.environ", {"XDG_CACHE_HOME": "/xdg/cache"}, clear=True):
            self.assertEqual(get_repo_cache_dir(), Path("/xdg/cache/repo"))

    def test_resolve_bazel_diff_cmd_options(self) -> None:
        cmd_explicit = resolve_bazel_diff_cmd(bazel_diff_bin="/path/to/bin")
        self.assertEqual(cmd_explicit, ["/path/to/bin"])

        with mock.patch.dict("os.environ", {"BAZEL_DIFF_BIN": "/env/bin"}, clear=True):
            self.assertEqual(resolve_bazel_diff_cmd(), ["/env/bin"])

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("shutil.which", return_value="/which/bin"),
        ):
            self.assertEqual(resolve_bazel_diff_cmd(), ["/which/bin"])

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("shutil.which", return_value=None),
        ):
            cmd = resolve_bazel_diff_cmd(bazel_output_root="/state/bazel")
            self.assertIn("--output_user_root=/state/bazel", cmd)
            self.assertIn("//src/bazel/tools:bazel-diff", cmd)

    def test_get_bazel_diff_version(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="bazel-diff 49.1.0\n")
            ver = get_bazel_diff_version(["/custom/bazel-diff"])
            self.assertEqual(ver, "bazel-diff 49.1.0")

    def test_is_working_tree_clean(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            self.assertTrue(is_working_tree_clean(self.repo_root))

            mock_run.return_value = MagicMock(returncode=0, stdout=" M file.py\n")
            self.assertFalse(is_working_tree_clean(self.repo_root))

    @patch("src.bazel.tools.diff.impacted.compute_impacted_targets")
    @patch("src.bazel.tools.diff.impacted.generate_hashes")
    @patch("src.bazel.tools.diff.impacted.resolve_commit_sha")
    @patch("src.bazel.tools.diff.impacted._run_cmd")
    def test_get_impacted_targets_uses_cached_base_hashes(
        self,
        mock_run: MagicMock,
        mock_resolve_sha: MagicMock,
        mock_gen_hashes: MagicMock,
        mock_compute: MagicMock,
    ) -> None:
        base_sha = "0123456789abcdef0123456789abcdef01234567"
        head_sha = "8888888888abcdef0123456789abcdef01234567"
        mock_resolve_sha.side_effect = lambda _repo, rev: base_sha if "HEAD~1" in rev else head_sha
        cache_dir = self.repo_root / "cache"
        base_cached = cache_dir / "bazel-diff" / f"{base_sha}.json"
        base_cached.parent.mkdir(parents=True)
        base_cached.write_text('{"hashes":{}}', encoding="utf-8")

        mock_compute.return_value = ImpactedResult(
            all_targets=["//src/app:app"],
            direct_targets=["//src/app:app"],
            targets=[ImpactedTarget(label="//src/app:app", target_distance=0, package_distance=0)],
        )

        with patch("src.bazel.tools.diff.impacted.is_working_tree_clean", return_value=True):
            res = get_impacted_targets(
                self.repo_root,
                "HEAD~1",
                repo_cache_dir=cache_dir,
                bazel_diff_bin="bazel-diff",
            )

        self.assertEqual(res.direct_targets, ["//src/app:app"])
        mock_run.assert_not_called()
        mock_gen_hashes.assert_called_once()
        mock_compute.assert_called_once()

    @patch("src.bazel.tools.diff.impacted.store")
    @patch("src.bazel.tools.diff.impacted.fetch")
    @patch("src.bazel.tools.diff.impacted.compute_impacted_targets")
    @patch("src.bazel.tools.diff.impacted.generate_hashes")
    @patch("src.bazel.tools.diff.impacted.resolve_commit_sha")
    @patch("src.bazel.tools.diff.impacted._run_cmd")
    def test_get_impacted_targets_uses_remote_cache_for_base(
        self,
        mock_run: MagicMock,
        mock_resolve_sha: MagicMock,
        mock_gen_hashes: MagicMock,
        mock_compute: MagicMock,
        mock_fetch: MagicMock,
        mock_store: MagicMock,
    ) -> None:
        base_sha = "base0123456789abcdef0123456789abcdef01"
        head_sha = "head0123456789abcdef0123456789abcdef01"
        mock_resolve_sha.side_effect = lambda _repo, rev: base_sha if "HEAD~1" in rev else head_sha
        cache_dir = self.repo_root / "cache"

        def fake_fetch(k: str, repo_root: Path | None = None) -> bytes | None:
            if base_sha in k:
                return b'{"hashes":{"//base": "1"}}'
            return None

        mock_fetch.side_effect = fake_fetch
        mock_compute.return_value = ImpactedResult([], [], [])

        with patch("src.bazel.tools.diff.impacted.is_working_tree_clean", return_value=True):
            res = get_impacted_targets(
                self.repo_root,
                "HEAD~1",
                repo_cache_dir=cache_dir,
                bazel_diff_bin="bazel-diff",
            )

        self.assertEqual(res.all_targets, [])
        mock_run.assert_not_called()
        mock_gen_hashes.assert_called_once()
        mock_store.assert_called_once()

    @patch("src.bazel.tools.diff.impacted.store")
    @patch("src.bazel.tools.diff.impacted.fetch", return_value=None)
    @patch("src.bazel.tools.diff.impacted.compute_impacted_targets")
    @patch("src.bazel.tools.diff.impacted.generate_hashes")
    @patch("src.bazel.tools.diff.impacted.resolve_commit_sha")
    @patch("src.bazel.tools.diff.impacted._run_cmd")
    def test_get_impacted_targets_creates_and_removes_worktree_on_cache_miss(
        self,
        mock_run: MagicMock,
        mock_resolve_sha: MagicMock,
        mock_gen_hashes: MagicMock,
        mock_compute: MagicMock,
        _mock_fetch: MagicMock,
        mock_store: MagicMock,
    ) -> None:
        base_sha = "abcdef0123456789abcdef0123456789abcdef01"
        head_sha = "fedcba0123456789abcdef0123456789abcdef01"
        mock_resolve_sha.side_effect = lambda _repo, rev: base_sha if "HEAD~1" in rev else head_sha
        cache_dir = self.repo_root / "cache"

        def fake_gen_hashes(ws: Path, out: Path, **kwargs: object) -> None:
            out.write_text('{"hashes":{}}', encoding="utf-8")

        mock_gen_hashes.side_effect = fake_gen_hashes
        mock_compute.return_value = ImpactedResult([], [], [])

        with patch("src.bazel.tools.diff.impacted.is_working_tree_clean", return_value=True):
            res = get_impacted_targets(
                self.repo_root,
                "HEAD~1",
                repo_cache_dir=cache_dir,
                bazel_diff_bin="bazel-diff",
            )

        self.assertEqual(res.all_targets, [])
        worktree_calls = [c for c in mock_run.call_args_list if "worktree" in c[0][0]]
        self.assertEqual(len(worktree_calls), 2)
        self.assertIn("add", worktree_calls[0][0][0])
        self.assertIn("remove", worktree_calls[1][0][0])
        self.assertTrue((cache_dir / "bazel-diff" / f"{base_sha}.json").exists())
        self.assertEqual(mock_store.call_count, 2)


if __name__ == "__main__":
    unittest.main()
