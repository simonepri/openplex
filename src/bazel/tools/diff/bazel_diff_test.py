"""Unit tests verifying Bazel diff calculation, core file escalation, target normalization, and package resolution."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

from src.bazel.tools.diff.bazel_diff import (
    CORE_FILES,
    BazelDiffResult,
    find_enclosing_package,
    get_bazel_diff,
    normalize_target_pattern,
    run_bazel_query,
)


class TestBazelDiff(unittest.TestCase):
    """Verifies baseline query execution and impacted target resolution."""

    @patch("subprocess.run")
    def test_run_bazel_query_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="//src/foo:bar\n//src/foo:bar_test\n",
        )
        res = run_bazel_query(Path("/repo"), "set(//src/foo:bar)")
        self.assertEqual(res, ["//src/foo:bar", "//src/foo:bar_test"])

    @patch("subprocess.run")
    def test_run_bazel_query_keep_going_code_3(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=3,
            stdout="ERROR: Skipping target\n//src/foo:valid_target\n",
        )
        res = run_bazel_query(Path("/repo"), "set(...)")
        self.assertEqual(res, ["//src/foo:valid_target"])

    @patch("subprocess.run")
    def test_run_bazel_query_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=7,
            stdout="",
        )
        res = run_bazel_query(Path("/repo"), "set(...)")
        self.assertEqual(res, [])

    @patch("src.bazel.tools.diff.bazel_diff.run_bazel_query")
    def test_get_bazel_diff_impact(self, mock_query: MagicMock) -> None:
        def query_side_effect(
            _repo_root: Path, expr: str, bazel_output_root: str | None = None
        ) -> list[str]:
            del bazel_output_root
            if expr.startswith("set("):
                return ["//src/foo:bar.py"]
            if "kind('.*_test'" in expr:
                return ["//src/foo:bar_test"]
            if "rdeps(" in expr:
                return ["//src/foo:bar.py", "//src/foo:bar_test", "//src/app:bin"]
            return []

        mock_query.side_effect = query_side_effect
        with patch.object(Path, "exists", return_value=True):
            res = get_bazel_diff(Path("/repo"), ["src/foo/bar.py"])
            self.assertEqual(res.direct_targets, ["//src/foo:bar.py"])
            self.assertEqual(res.affected_tests, ["//src/foo:bar_test"])
            self.assertIn("//src/app:bin", res.affected_targets)


class CoreFilesEscalationTest(unittest.TestCase):
    """Verifies that changes to core build infrastructure files escalate to global workspace invalidation."""

    def test_root_core_file_change_escalates_to_global_workspace_invalidation(self) -> None:
        repo_root = Path("/fake/workspace")
        for core_file in sorted(CORE_FILES):
            with self.subTest(file=core_file):
                diff = get_bazel_diff(repo_root, [core_file])
                self.assertTrue(diff.is_global)
                self.assertEqual(diff.direct_targets, ["//..."])
                self.assertEqual(diff.affected_targets, ["//..."])
                self.assertEqual(diff.affected_tests, ["//..."])
                self.assertEqual(diff.affected_packages, ["//..."])

    def test_prefixed_dot_slash_core_file_escalates_to_global_workspace_invalidation(self) -> None:
        repo_root = Path("/fake/workspace")
        for core_file in sorted(CORE_FILES):
            prefixed = f"./{core_file}"
            with self.subTest(file=prefixed):
                diff = get_bazel_diff(repo_root, [prefixed])
                self.assertTrue(diff.is_global)
                self.assertEqual(diff.direct_targets, ["//..."])
                self.assertEqual(diff.affected_targets, ["//..."])
                self.assertEqual(diff.affected_tests, ["//..."])
                self.assertEqual(diff.affected_packages, ["//..."])

    def test_nested_file_matching_core_file_name_does_not_escalate_to_global(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            nested_pkg = repo_root / "src" / "pkg"
            nested_pkg.mkdir(parents=True)
            nested_file = nested_pkg / "BUILD.bazel"
            nested_file.write_text('py_library(name = "pkg")\n', encoding="utf-8")

            with mock.patch("src.bazel.tools.diff.bazel_diff.run_bazel_query", return_value=[]):
                diff = get_bazel_diff(repo_root, ["src/pkg/BUILD.bazel"])

            self.assertFalse(diff.is_global)
            self.assertEqual(diff.direct_targets, [])
            self.assertEqual(diff.affected_packages, ["//src/pkg/..."])

    def test_unrelated_root_file_does_not_escalate_to_global_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            readme = repo_root / "readme.md"
            readme.write_text("# Readme\n", encoding="utf-8")
            (repo_root / "BUILD.bazel").write_text("", encoding="utf-8")

            with mock.patch("src.bazel.tools.diff.bazel_diff.run_bazel_query", return_value=[]):
                diff = get_bazel_diff(repo_root, ["readme.md"])

            self.assertFalse(diff.is_global)
            self.assertEqual(diff.direct_targets, [])
            self.assertEqual(diff.affected_packages, ["//..."])


class RdepsQueryingTest(unittest.TestCase):
    """Verifies dependency graph querying using mocked Bazel invocations."""

    def test_query_execution_constructs_rdeps_and_kind_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            src_file = repo_root / "src" / "lib.py"
            src_file.parent.mkdir(parents=True)
            src_file.write_text("x = 1\n", encoding="utf-8")
            (src_file.parent / "BUILD.bazel").write_text("", encoding="utf-8")

            queries_executed: list[str] = []

            def fake_query(
                root: Path, expr: str, bazel_output_root: str | None = None
            ) -> list[str]:
                del root, bazel_output_root
                queries_executed.append(expr)
                if expr.startswith("set("):
                    return ["//src:lib"]
                if expr.startswith("rdeps("):
                    return ["//src:lib", "//src:lib_test"]
                if expr.startswith("kind("):
                    return ["//src:lib_test"]
                return []

            with mock.patch(
                "src.bazel.tools.diff.bazel_diff.run_bazel_query", side_effect=fake_query
            ):
                diff = get_bazel_diff(repo_root, ["src/lib.py"], max_rdeps_depth=10)

            self.assertEqual(
                queries_executed,
                [
                    'set("src/lib.py")',
                    "rdeps(//..., set(//src:lib), 10)",
                    "kind('.*_test', rdeps(//..., set(//src:lib), 10))",
                ],
            )
            self.assertEqual(diff.direct_targets, ["//src:lib"])
            self.assertEqual(diff.affected_targets, ["//src:lib", "//src:lib_test"])
            self.assertEqual(diff.affected_tests, ["//src:lib_test"])
            self.assertEqual(diff.affected_packages, ["//src/..."])
            self.assertFalse(diff.is_global)

    def test_custom_rdeps_depth_propagates_to_query_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            src_file = repo_root / "src" / "service.py"
            src_file.parent.mkdir(parents=True)
            src_file.write_text("def run(): pass\n", encoding="utf-8")
            (src_file.parent / "BUILD.bazel").write_text("", encoding="utf-8")

            queries_executed: list[str] = []

            def fake_query(
                root: Path, expr: str, bazel_output_root: str | None = None
            ) -> list[str]:
                del root, bazel_output_root
                queries_executed.append(expr)
                return ["//src:service"] if expr.startswith("set(") else []

            with mock.patch(
                "src.bazel.tools.diff.bazel_diff.run_bazel_query", side_effect=fake_query
            ):
                get_bazel_diff(repo_root, ["src/service.py"], max_rdeps_depth=4)

            self.assertIn("rdeps(//..., set(//src:service), 4)", queries_executed)
            self.assertIn("kind('.*_test', rdeps(//..., set(//src:service), 4))", queries_executed)

    def test_bazel_output_root_propagates_to_query_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            src_file = repo_root / "main.py"
            src_file.write_text("print(1)\n", encoding="utf-8")
            (repo_root / "BUILD.bazel").write_text("", encoding="utf-8")

            output_roots: list[str | None] = []

            def fake_query(
                root: Path, expr: str, bazel_output_root: str | None = None
            ) -> list[str]:
                del root, expr
                output_roots.append(bazel_output_root)
                return ["//:main"]

            with mock.patch(
                "src.bazel.tools.diff.bazel_diff.run_bazel_query", side_effect=fake_query
            ):
                get_bazel_diff(repo_root, ["main.py"], bazel_output_root="/custom/cache/root")

            self.assertTrue(all(root == "/custom/cache/root" for root in output_roots))

    def test_empty_direct_targets_skips_rdeps_queries_and_returns_empty_affected_lists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            doc_file = repo_root / "docs" / "guide.md"
            doc_file.parent.mkdir(parents=True)
            doc_file.write_text("# Guide\n", encoding="utf-8")
            (doc_file.parent / "BUILD.bazel").write_text("", encoding="utf-8")

            queries_executed: list[str] = []

            def fake_query(
                root: Path, expr: str, bazel_output_root: str | None = None
            ) -> list[str]:
                del root, bazel_output_root
                queries_executed.append(expr)
                return []

            with mock.patch(
                "src.bazel.tools.diff.bazel_diff.run_bazel_query", side_effect=fake_query
            ):
                diff = get_bazel_diff(repo_root, ["docs/guide.md"])

            self.assertEqual(queries_executed, ['set("docs/guide.md")'])
            self.assertEqual(diff.direct_targets, [])
            self.assertEqual(diff.affected_targets, [])
            self.assertEqual(diff.affected_tests, [])
            self.assertEqual(diff.affected_packages, ["//docs/..."])
            self.assertFalse(diff.is_global)


class RunBazelQuerySubprocessTest(unittest.TestCase):
    """Verifies low-level subprocess execution and error isolation for Bazel queries."""

    def test_successful_query_returns_stripped_labels(self) -> None:
        stdout_output = "//src/core:core\n//src/core:core_test\n@rules_python//python:defs.bzl\n"
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["bazel", "query"],
                returncode=0,
                stdout=stdout_output,
                stderr="",
            ),
        ) as mock_run:
            targets = run_bazel_query(
                Path("/workspace"),
                "//src/core/...",
                bazel_output_root="/tmp/bazel_cache",
            )

        self.assertEqual(
            targets,
            ["//src/core:core", "//src/core:core_test", "@rules_python//python:defs.bzl"],
        )
        mock_run.assert_called_once_with(
            [
                "bazel",
                "--output_user_root=/tmp/bazel_cache",
                "query",
                "--keep_going",
                "--ui_event_filters=-info,-stdout",
                "--noshow_progress",
                "//src/core/...",
            ],
            cwd=Path("/workspace"),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_partial_query_with_exit_code_three_returns_parsed_labels(self) -> None:
        stdout_output = "//src/a:target\n//src/b:target\n"
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["bazel", "query"],
                returncode=3,
                stdout=stdout_output,
                stderr="ERROR: error in package",
            ),
        ):
            targets = run_bazel_query(Path("/workspace"), "//...")

        self.assertEqual(targets, ["//src/a:target", "//src/b:target"])

    def test_failed_query_with_unhandled_exit_code_returns_empty_list(self) -> None:
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["bazel", "query"],
                returncode=1,
                stdout="//should:not_return\n",
                stderr="Fatal syntax error",
            ),
        ):
            targets = run_bazel_query(Path("/workspace"), "invalid syntax query")

        self.assertEqual(targets, [])

    def test_os_error_during_query_returns_empty_list(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("bazel binary missing")):
            targets = run_bazel_query(Path("/workspace"), "//...")

        self.assertEqual(targets, [])


class TargetNormalizationTest(unittest.TestCase):
    """Verifies target pattern normalization into canonical Bazel expressions."""

    def test_normalization_converts_workspace_aliases_to_all_wildcard(self) -> None:
        repo_root = Path("/workspace")
        test_cases = [".", "./", "//..."]
        for target in test_cases:
            with self.subTest(target=target):
                self.assertEqual(normalize_target_pattern(repo_root, target), "//...")

    def test_normalization_preserves_canonical_bazel_labels_and_wildcards(self) -> None:
        repo_root = Path("/workspace")
        test_cases = [
            "//pkg:target",
            ":local_target",
            "//pkg/subpkg:sub_target",
            "//src/...",
            "src/...",
        ]
        for target in test_cases:
            with self.subTest(target=target):
                self.assertEqual(normalize_target_pattern(repo_root, target), target)

    def test_normalization_converts_directories_to_package_wildcards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            pkg_dir = repo_root / "src" / "tools"
            pkg_dir.mkdir(parents=True)

            self.assertEqual(
                normalize_target_pattern(repo_root, "src/tools"),
                "//src/tools/...",
            )
            self.assertEqual(
                normalize_target_pattern(repo_root, "./src/tools"),
                "//src/tools/...",
            )

    def test_normalization_converts_files_to_canonical_target_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            nested_file = repo_root / "src" / "tools" / "app.py"
            nested_file.parent.mkdir(parents=True)
            nested_file.write_text("print('test')\n", encoding="utf-8")

            root_file = repo_root / "entrypoint.sh"
            root_file.write_text("#!/bin/sh\n", encoding="utf-8")

            self.assertEqual(
                normalize_target_pattern(repo_root, "src/tools/app.py"),
                "//src/tools:app.py",
            )
            self.assertEqual(
                normalize_target_pattern(repo_root, "./src/tools/app.py"),
                "//src/tools:app.py",
            )
            self.assertEqual(
                normalize_target_pattern(repo_root, "entrypoint.sh"),
                "//:entrypoint.sh",
            )
            self.assertEqual(
                normalize_target_pattern(repo_root, "./entrypoint.sh"),
                "//:entrypoint.sh",
            )

    def test_normalization_preserves_unrecognized_non_filesystem_targets(self) -> None:
        repo_root = Path("/workspace")
        self.assertEqual(
            normalize_target_pattern(repo_root, "non_existent_folder"),
            "non_existent_folder",
        )


class PackageResolutionTest(unittest.TestCase):
    """Verifies enclosing package resolution across directories and ancestor packages."""

    def test_resolution_identifies_direct_parent_package_with_build_bazel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            pkg_dir = repo_root / "src" / "service"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "BUILD.bazel").write_text("", encoding="utf-8")
            source_file = pkg_dir / "service.py"
            source_file.write_text("pass\n", encoding="utf-8")

            pkg = find_enclosing_package(repo_root, "src/service/service.py")
            self.assertEqual(pkg, "//src/service/...")

    def test_resolution_identifies_direct_parent_package_with_legacy_build_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            pkg_dir = repo_root / "legacy" / "pkg"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "BUILD").write_text("", encoding="utf-8")
            source_file = pkg_dir / "legacy.go"
            source_file.write_text("package pkg\n", encoding="utf-8")

            pkg = find_enclosing_package(repo_root, "legacy/pkg/legacy.go")
            self.assertEqual(pkg, "//legacy/pkg/...")

    def test_resolution_walks_up_to_nearest_ancestor_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            ancestor_pkg = repo_root / "src" / "domain"
            deep_dir = ancestor_pkg / "sub" / "nested"
            deep_dir.mkdir(parents=True)
            (ancestor_pkg / "BUILD.bazel").write_text("", encoding="utf-8")
            deep_file = deep_dir / "schema.json"
            deep_file.write_text("{}\n", encoding="utf-8")

            pkg = find_enclosing_package(repo_root, "src/domain/sub/nested/schema.json")
            self.assertEqual(pkg, "//src/domain/...")

    def test_resolution_walks_up_to_root_package_when_intermediate_packages_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / "BUILD.bazel").write_text("", encoding="utf-8")
            deep_file = repo_root / "unowned" / "file.txt"
            deep_file.parent.mkdir(parents=True)
            deep_file.write_text("text\n", encoding="utf-8")

            pkg = find_enclosing_package(repo_root, "unowned/file.txt")
            self.assertEqual(pkg, "//...")

    def test_resolution_returns_none_when_no_build_file_exists_in_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            target_file = repo_root / "a" / "b" / "c.py"
            target_file.parent.mkdir(parents=True)
            target_file.write_text("print(1)\n", encoding="utf-8")

            pkg = find_enclosing_package(repo_root, "a/b/c.py")
            self.assertIsNone(pkg)

    def test_missing_on_disk_files_are_filtered_before_diff_calculation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            existing_file = repo_root / "src" / "existing.py"
            existing_file.parent.mkdir(parents=True)
            existing_file.write_text("x = 1\n", encoding="utf-8")
            (existing_file.parent / "BUILD.bazel").write_text("", encoding="utf-8")

            queries_executed: list[str] = []

            def fake_query(
                root: Path, expr: str, bazel_output_root: str | None = None
            ) -> list[str]:
                del root, bazel_output_root
                queries_executed.append(expr)
                return ["//src:existing"]

            with mock.patch(
                "src.bazel.tools.diff.bazel_diff.run_bazel_query", side_effect=fake_query
            ):
                diff = get_bazel_diff(
                    repo_root,
                    ["src/existing.py", "src/deleted.py", "untracked/missing.go"],
                )

            self.assertEqual(queries_executed[0], 'set("src/existing.py")')
            self.assertEqual(diff.direct_targets, ["//src:existing"])
            self.assertEqual(diff.affected_packages, ["//src/..."])
            self.assertFalse(diff.is_global)

    def test_empty_or_all_missing_files_returns_empty_diff_result(self) -> None:
        repo_root = Path("/workspace")
        diff_empty = get_bazel_diff(repo_root, [])
        self.assertEqual(
            diff_empty,
            BazelDiffResult(
                direct_targets=[],
                affected_targets=[],
                affected_tests=[],
                affected_packages=[],
                is_global=False,
            ),
        )

        diff_missing = get_bazel_diff(repo_root, ["non_existent_file.py"])
        self.assertEqual(
            diff_missing,
            BazelDiffResult(
                direct_targets=[],
                affected_targets=[],
                affected_tests=[],
                affected_packages=[],
                is_global=False,
            ),
        )


if __name__ == "__main__":
    unittest.main()
