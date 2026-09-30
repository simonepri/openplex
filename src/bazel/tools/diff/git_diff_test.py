"""Unit tests verifying Git baseline detection, changed file discovery, and structured diff parsing."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from src.bazel.tools.diff.git_diff import (
        FileDiff,
        GitDiffResult,
        detect_git_baseline,
        get_changed_files,
        get_git_diff,
        is_ci_push_to_main,
        parse_name_status,
        parse_numstat,
        split_patch_by_file,
    )
except ImportError:  # pragma: no cover
    from git_diff import (  # type: ignore[no-redef]
        FileDiff,
        GitDiffResult,
        detect_git_baseline,
        get_changed_files,
        get_git_diff,
        is_ci_push_to_main,
        parse_name_status,
        parse_numstat,
        split_patch_by_file,
    )


def _init_repo(path: Path) -> None:
    """Initialize a git repository with default user configuration."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True, capture_output=True)


def _commit(repo: Path, rel_path: str, content: str, message: str) -> str:
    """Write a file, stage it, commit it, and return the commit SHA."""
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "add", rel_path], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


class CiPushToMainTest(unittest.TestCase):
    """Verifies detection of BuildBuddy Workflows and GitHub Actions push runs on main."""

    def test_push_to_main_is_detected(self) -> None:
        env = {"CI": "true", "GIT_BRANCH": "main", "GIT_PR_NUMBER": "0"}
        self.assertTrue(is_ci_push_to_main(env))

    def test_pull_request_is_not_detected(self) -> None:
        env = {"CI": "true", "GIT_BRANCH": "main", "GIT_PR_NUMBER": "12"}
        self.assertFalse(is_ci_push_to_main(env))

    def test_push_to_other_branch_is_not_detected(self) -> None:
        env = {"CI": "true", "GIT_BRANCH": "feature", "GIT_PR_NUMBER": "0"}
        self.assertFalse(is_ci_push_to_main(env))

    def test_local_run_is_not_detected(self) -> None:
        self.assertFalse(is_ci_push_to_main({}))
        self.assertFalse(is_ci_push_to_main({"GIT_BRANCH": "main"}))

    def test_github_push_to_main_is_detected(self) -> None:
        env = {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF_NAME": "main",
        }
        self.assertTrue(is_ci_push_to_main(env))

    def test_github_pull_request_is_not_detected(self) -> None:
        env = {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF_NAME": "12/merge",
        }
        self.assertFalse(is_ci_push_to_main(env))

    def test_github_push_to_other_branch_is_not_detected(self) -> None:
        env = {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF_NAME": "feature",
        }
        self.assertFalse(is_ci_push_to_main(env))

    def test_github_dispatch_on_main_is_not_detected(self) -> None:
        env = {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF_NAME": "main",
        }
        self.assertFalse(is_ci_push_to_main(env))


class ChangedFilesDiscoveryTest(unittest.TestCase):
    """Verifies change detection across clean, staged, unstaged, untracked, and branch states."""

    def test_clean_working_tree_reports_no_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "initial.txt", "init\n", "Initial commit")
            changed = get_changed_files(repo)
            self.assertEqual(changed, [])

    def test_staged_file_is_detected_in_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "initial.txt", "init\n", "Initial commit")
            staged = repo / "staged_file.txt"
            staged.write_text("staged content\n")
            subprocess.run(
                ["git", "add", "staged_file.txt"], cwd=repo, check=True, capture_output=True
            )
            changed = get_changed_files(repo)
            self.assertEqual(changed, ["staged_file.txt"])

    def test_unstaged_dirty_file_is_detected_in_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "tracked.txt", "original\n", "Commit tracked")
            (repo / "tracked.txt").write_text("modified content\n")
            changed = get_changed_files(repo)
            self.assertEqual(changed, ["tracked.txt"])

    def test_untracked_file_is_detected_in_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "initial.txt", "init\n", "Initial commit")
            (repo / "untracked.txt").write_text("untracked\n")
            changed = get_changed_files(repo)
            self.assertEqual(changed, ["untracked.txt"])

    def test_branch_commit_is_detected_in_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "main.txt", "main content\n", "Main commit")
            subprocess.run(
                ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
            )
            _commit(repo, "feature.txt", "feature content\n", "Feature commit")
            changed = get_changed_files(repo)
            self.assertEqual(changed, ["feature.txt"])

    def test_multiple_change_categories_are_deduplicated_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "base.txt", "base\n", "Base commit")
            subprocess.run(
                ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
            )
            _commit(repo, "branch_file.txt", "branch content\n", "Branch commit")

            (repo / "staged.txt").write_text("staged\n")
            subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True, capture_output=True)

            (repo / "base.txt").write_text("base modified\n")
            (repo / "untracked.txt").write_text("untracked\n")

            changed = get_changed_files(repo)
            self.assertEqual(
                changed, ["base.txt", "branch_file.txt", "staged.txt", "untracked.txt"]
            )


class BaselineDetectionTest(unittest.TestCase):
    """Verifies baseline ref detection with merge-base and fallback paths."""

    def test_branch_merge_base_detected_against_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            base_sha = _commit(repo, "init.txt", "init\n", "Initial commit")
            subprocess.run(
                ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
            )
            _commit(repo, "feat.txt", "feat\n", "Feat commit")

            base, target = detect_git_baseline(repo)
            self.assertEqual(base, base_sha)
            self.assertEqual(target, "HEAD")

    def test_branch_merge_base_detected_against_origin_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            c1 = _commit(repo, "init.txt", "init\n", "Initial commit")
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", c1],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            _commit(repo, "init.txt", "init\nupdate\n", "Second commit on main")
            subprocess.run(
                ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
            )
            _commit(repo, "feat.txt", "feat\n", "Feat commit")

            base, target = detect_git_baseline(repo)
            self.assertEqual(base, c1)
            self.assertEqual(target, "HEAD")

    def test_clean_main_falls_back_to_head_minus_one(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "file.txt", "v1\n", "Commit 1")
            _commit(repo, "file.txt", "v1\nv2\n", "Commit 2")

            base, target = detect_git_baseline(repo)
            self.assertEqual(base, "HEAD~1")
            self.assertEqual(target, "HEAD")

    def test_single_commit_repo_falls_back_to_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "file.txt", "v1\n", "Single commit")

            base, target = detect_git_baseline(repo)
            self.assertEqual(base, "HEAD")
            self.assertEqual(target, "HEAD")


class GitDiffExecutionTest(unittest.TestCase):
    """Verifies git diff execution, FileDiff structure, and aggregate metrics."""

    def test_identical_base_and_target_returns_empty_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "f.txt", "hello\n", "Init")

            result = get_git_diff(repo, base_ref="HEAD", target_ref="HEAD")
            self.assertIsInstance(result, GitDiffResult)
            self.assertEqual(result.files, [])
            self.assertEqual(result.changed_paths, [])
            self.assertEqual(result.total_additions, 0)
            self.assertEqual(result.total_deletions, 0)
            self.assertEqual(result.raw_patch, "")

    def test_git_diff_parses_additions_deletions_and_hunks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "a.txt", "line1\nline2\nline3\n", "Commit 1")
            (repo / "b.txt").write_text("b1\nb2\n")
            subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Add b"], cwd=repo, check=True, capture_output=True
            )

            (repo / "a.txt").write_text("line1\nline2_mod\nline3\nline4_new\n")
            (repo / "c.txt").write_text("c1\nc2\n")
            subprocess.run(["git", "rm", "b.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "add", "a.txt", "c.txt"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "Commit 2"], cwd=repo, check=True, capture_output=True
            )

            result = get_git_diff(repo, base_ref="HEAD~1", target_ref="HEAD")
            self.assertEqual(result.changed_paths, ["a.txt", "b.txt", "c.txt"])
            file_map = {f.path: f for f in result.files}

            self.assertEqual(file_map["a.txt"].status, "M")
            self.assertEqual(file_map["a.txt"].additions, 2)
            self.assertEqual(file_map["a.txt"].deletions, 1)
            self.assertIn("+line4_new", file_map["a.txt"].patch)

            self.assertEqual(file_map["b.txt"].status, "D")
            self.assertEqual(file_map["b.txt"].deletions, 2)

            self.assertEqual(file_map["c.txt"].status, "A")
            self.assertEqual(file_map["c.txt"].additions, 2)
            self.assertIn("+c1", file_map["c.txt"].patch)

            self.assertEqual(result.total_additions, 4)
            self.assertEqual(result.total_deletions, 3)

    def test_git_diff_parses_file_rename_with_old_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "original.txt", "original text\n", "Init commit")
            subprocess.run(
                ["git", "mv", "original.txt", "renamed.txt"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            (repo / "renamed.txt").write_text("original text\nextra\n")
            subprocess.run(["git", "add", "renamed.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Rename file"], cwd=repo, check=True, capture_output=True
            )

            result = get_git_diff(repo, base_ref="HEAD~1", target_ref="HEAD")
            self.assertEqual(result.changed_paths, ["renamed.txt"])
            renamed = result.files[0]
            self.assertIsInstance(renamed, FileDiff)
            self.assertEqual(renamed.status, "R")
            self.assertEqual(renamed.path, "renamed.txt")
            self.assertEqual(renamed.old_path, "original.txt")
            self.assertEqual(renamed.additions, 1)

    def test_git_diff_resolves_baseline_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            _commit(repo, "main.txt", "main\n", "Initial commit")
            subprocess.run(
                ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
            )
            _commit(repo, "branch.txt", "branch\n", "Feature commit")

            result = get_git_diff(repo)
            self.assertEqual(result.changed_paths, ["branch.txt"])
            self.assertEqual(result.files[0].status, "A")


class DiffParsingHelpersTest(unittest.TestCase):
    """Verifies output parsers for git diff --numstat, --name-status, and unified patch chunks."""

    def test_parse_numstat_handles_regular_rename_and_binary(self) -> None:
        raw = "10\t5\tsrc/file.py\n1\t1\tsrc/{old.py => new.py}\n-\t-\timage.png\n"
        stats = parse_numstat(raw)
        self.assertEqual(stats["src/file.py"], (10, 5, None))
        self.assertEqual(stats["src/new.py"], (1, 1, "src/old.py"))
        self.assertEqual(stats["image.png"], (0, 0, None))

    def test_parse_name_status_handles_modes_and_renames(self) -> None:
        raw = "M\tmodified.py\nA\tadded.py\nD\tdeleted.py\nR100\told.py\tnew.py\n"
        status_map = parse_name_status(raw)
        self.assertEqual(status_map["modified.py"], ("M", None))
        self.assertEqual(status_map["added.py"], ("A", None))
        self.assertEqual(status_map["deleted.py"], ("D", None))
        self.assertEqual(status_map["new.py"], ("R", "old.py"))

    def test_split_patch_by_file_separates_hunks_per_target_file(self) -> None:
        raw = (
            "diff --git a/f1.py b/f1.py\n"
            "--- a/f1.py\n"
            "+++ b/f1.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/f2.py b/f2.py\n"
            "--- a/f2.py\n"
            "+++ b/f2.py\n"
            "@@ -1 +1 @@\n"
            "+added\n"
        )
        patches = split_patch_by_file(raw)
        self.assertIn("f1.py", patches)
        self.assertIn("f2.py", patches)
        self.assertTrue(patches["f1.py"].startswith("diff --git a/f1.py"))
        self.assertTrue(patches["f2.py"].startswith("diff --git a/f2.py"))


if __name__ == "__main__":
    unittest.main()
