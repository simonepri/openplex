"""Analyzes Git diff outputs between branches or commits to extract changed files, patch hunks, and line statistics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.bazel.tools.diff import git_diff as _engine
from src.bazel.tools.diff.git_diff import (
    FileDiff,
    GitDiffResult,
    detect_git_baseline,
    parse_name_status,
    parse_numstat,
    split_patch_by_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def get_git_diff(
    repo_root: Path,
    base: str | None = None,
    target: str | None = None,
) -> GitDiffResult:
    """Collect git diff between base and target refs or working tree.

    If base is None, auto-detects based on repo state.
    """
    return _engine.get_git_diff(repo_root, base_ref=base, target_ref=target)


__all__ = [
    "FileDiff",
    "GitDiffResult",
    "detect_git_baseline",
    "get_git_diff",
    "parse_name_status",
    "parse_numstat",
    "split_patch_by_file",
]
