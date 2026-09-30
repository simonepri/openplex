"""Unified Git and Bazel diff analysis and execution engines."""

from __future__ import annotations

from .bazel_diff import (
    BazelDiffResult,
    BazelQueryError,
    get_bazel_diff,
    normalize_target_pattern,
    run_bazel_query,
)
from .cli import (
    ParsedArgs,
    dispatch,
    main,
    parse_cli_args,
    run_check,
    run_fix,
    run_publish,
    run_test,
)
from .gate_diff import filter_gates, filter_generators
from .git_diff import (
    FileDiff,
    GitDiffResult,
    detect_git_baseline,
    get_changed_files,
    is_ci_push_to_main,
    get_git_diff,
    parse_name_status,
    parse_numstat,
    split_patch_by_file,
)

__all__ = [
    "BazelDiffResult",
    "BazelQueryError",
    "FileDiff",
    "GitDiffResult",
    "ParsedArgs",
    "detect_git_baseline",
    "dispatch",
    "filter_gates",
    "filter_generators",
    "get_bazel_diff",
    "get_changed_files",
    "is_ci_push_to_main",
    "get_git_diff",
    "main",
    "normalize_target_pattern",
    "parse_cli_args",
    "parse_name_status",
    "parse_numstat",
    "run_bazel_query",
    "run_check",
    "run_fix",
    "run_publish",
    "run_test",
    "split_patch_by_file",
]
