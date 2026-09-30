"""Tests Git diff parsing, hunk extraction, file change status categorization, and patch statistics calculations."""

import unittest

from src.infra.tools.review.context import git_diff


class TestGitDiff(unittest.TestCase):
    def test_parse_numstat_simple(self) -> None:
        raw = "10\t5\tsrc/foo/bar.py\n20\t0\tsrc/new.py\n"
        stats = git_diff.parse_numstat(raw)
        self.assertEqual(stats["src/foo/bar.py"], (10, 5, None))
        self.assertEqual(stats["src/new.py"], (20, 0, None))

    def test_parse_numstat_rename(self) -> None:
        raw = "1\t1\tsrc/{old.py => new.py}\n"
        stats = git_diff.parse_numstat(raw)
        self.assertIn("src/new.py", stats)
        added, deleted, old = stats["src/new.py"]
        self.assertEqual(added, 1)
        self.assertEqual(deleted, 1)
        self.assertEqual(old, "src/old.py")

    def test_parse_name_status(self) -> None:
        raw = "M\tsrc/file1.py\nA\tsrc/file2.py\nR100\tsrc/old.py\tsrc/new.py\n"
        status = git_diff.parse_name_status(raw)
        self.assertEqual(status["src/file1.py"], ("M", None))
        self.assertEqual(status["src/file2.py"], ("A", None))
        self.assertEqual(status["src/new.py"], ("R", "src/old.py"))

    def test_split_patch_by_file(self) -> None:
        patch_text = (
            "diff --git a/file1.py b/file1.py\n"
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1 +1 @@\n"
            "-foo\n"
            "+bar\n"
            "diff --git a/file2.py b/file2.py\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -1 +1 @@\n"
            "+hello\n"
        )
        patches = git_diff.split_patch_by_file(patch_text)
        self.assertIn("file1.py", patches)
        self.assertIn("file2.py", patches)
        self.assertTrue(patches["file1.py"].startswith("diff --git a/file1.py"))
        self.assertTrue(patches["file2.py"].startswith("diff --git a/file2.py"))


if __name__ == "__main__":
    unittest.main()
