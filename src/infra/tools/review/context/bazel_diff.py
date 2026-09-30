"""Queries Bazel dependency graphs to identify impacted build targets, reverse dependencies, and affected tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.bazel.tools.diff import bazel_diff as _engine
from src.bazel.tools.diff.bazel_diff import BazelDiffResult, BazelQueryError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def run_bazel_query(
    repo_root: Path,
    query_expr: str,
    bazel_output_root: str | None = None,
    output: str = "label",
) -> list[str]:
    """Execute a bazel query command and return trimmed output lines."""
    return _engine.run_bazel_query(
        repo_root, query_expr, bazel_output_root=bazel_output_root, output=output
    )


def get_bazel_diff(
    repo_root: Path,
    changed_files: Sequence[str],
    max_rdeps_depth: int = 10,
    bazel_output_root: str | None = None,
) -> BazelDiffResult:
    """Maps changed files to Bazel targets and downstream affected tests."""
    return _engine.get_bazel_diff(
        repo_root,
        changed_files,
        max_rdeps_depth=max_rdeps_depth,
        bazel_output_root=bazel_output_root,
        query_fn=run_bazel_query,
    )


__all__ = [
    "BazelDiffResult",
    "BazelQueryError",
    "get_bazel_diff",
    "run_bazel_query",
]
