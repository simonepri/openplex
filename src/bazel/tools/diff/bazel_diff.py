"""Analyze repository changes to determine affected Bazel targets, tests, and packages."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

CORE_FILES: frozenset[str] = frozenset({
    ".bazelrc",
    ".bazelversion",
    "BUILD.bazel",
    "MODULE.bazel",
    "MODULE.bazel.lock",
    "multitool.lock.json",
    "requirements_dev_lock.txt",
    "requirements_lock.txt",
    "src/bazel/profiles/profiles.bazelrc",
})


@dataclasses.dataclass(frozen=True)
class BazelDiffResult:
    """Represents the targets, tests, and packages affected by workspace changes."""

    direct_targets: list[str]
    affected_targets: list[str]
    affected_tests: list[str]
    affected_packages: list[str] = dataclasses.field(default_factory=list)
    is_global: bool = False


def get_bazel_diff(
    repo_root: Path,
    changed_files: Sequence[str],
    max_rdeps_depth: int = 10,
    bazel_output_root: str | None = None,
    query_fn: Callable[..., list[str]] | None = None,
) -> BazelDiffResult:
    """Determine direct, affected targets, tests, and packages for changed workspace files."""
    run_query = query_fn or run_bazel_query
    if any(_is_core_file(repo_root, f) for f in changed_files):
        return BazelDiffResult(
            direct_targets=["//..."],
            affected_targets=["//..."],
            affected_tests=["//..."],
            affected_packages=["//..."],
            is_global=True,
        )

    valid_files: list[str] = []
    for f in changed_files:
        path = Path(f)
        if path.is_absolute():
            try:
                rel = path.relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                try:
                    rel = path.relative_to(repo_root).as_posix()
                except ValueError:
                    continue
        else:
            rel = f.removeprefix("./")

        target_path = repo_root / rel
        if _file_exists(target_path):
            valid_files.append(rel)

    if not valid_files:
        return BazelDiffResult(
            direct_targets=[],
            affected_targets=[],
            affected_tests=[],
            affected_packages=[],
            is_global=False,
        )

    file_set = " ".join(f'"{f}"' for f in valid_files)
    direct_targets = sorted(
        set(
            run_query(
                repo_root,
                f"set({file_set})",
                bazel_output_root=bazel_output_root,
            )
        )
    )

    if direct_targets:
        target_set = " ".join(direct_targets)
        rdeps_expr = f"rdeps(//..., set({target_set}), {max_rdeps_depth})"
        affected_targets = sorted(
            set(
                run_query(
                    repo_root,
                    rdeps_expr,
                    bazel_output_root=bazel_output_root,
                )
            )
        )
        tests_expr = f"kind('.*_test', rdeps(//..., set({target_set}), {max_rdeps_depth}))"
        affected_tests = sorted(
            set(
                run_query(
                    repo_root,
                    tests_expr,
                    bazel_output_root=bazel_output_root,
                )
            )
        )
    else:
        affected_targets = []
        affected_tests = []

    packages: set[str] = set()
    for f in valid_files:
        pkg = find_enclosing_package(repo_root, f)
        if pkg:
            packages.add(pkg)
    affected_packages = sorted(packages)

    return BazelDiffResult(
        direct_targets=direct_targets,
        affected_targets=affected_targets,
        affected_tests=affected_tests,
        affected_packages=affected_packages,
        is_global=False,
    )


def run_bazel_query(
    repo_root: Path,
    query_expr: str,
    bazel_output_root: str | None = None,
) -> list[str]:
    """Execute bazel query with standard flags and return target labels."""
    cmd = ["bazel"]
    output_root = bazel_output_root or os.environ.get("BAZEL_OUTPUT_ROOT")
    if output_root:
        cmd.append(f"--output_user_root={output_root}")
    cmd.extend([
        "query",
        "--keep_going",
        "--ui_event_filters=-info,-stdout",
        "--noshow_progress",
        query_expr,
    ])
    try:
        res = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode not in {0, 3}:
            return []
        return [
            line.strip()
            for line in res.stdout.splitlines()
            if line.strip() and line.strip().startswith(("//", "@"))
        ]
    except OSError:
        return []


def normalize_target_pattern(repo_root: Path, target: str) -> str:
    """Normalize a target pattern or path to a valid Bazel target pattern."""
    if target in {".", "./", "//..."}:
        return "//..."
    if target.startswith(("//", ":")) or target.endswith("..."):
        return target

    clean_target = target.removeprefix("./")
    local_path = repo_root / clean_target
    if local_path.is_dir():
        rel = local_path.relative_to(repo_root).as_posix()
        return "//..." if rel in {".", ""} else f"//{rel}/..."
    if _file_exists(local_path):
        rel = local_path.relative_to(repo_root).as_posix()
        pkg = local_path.parent.relative_to(repo_root).as_posix()
        return f"//:{local_path.name}" if pkg in {".", ""} else f"//{pkg}:{local_path.name}"

    return target


def find_enclosing_package(repo_root: Path, file_path: str) -> str | None:
    """Find the nearest enclosing Bazel package pattern for a given file path."""
    clean = file_path.removeprefix("./")
    target_path = repo_root / clean
    current = target_path.parent
    root_resolved = repo_root.resolve()

    while True:
        try:
            rel = current.resolve().relative_to(root_resolved)
        except ValueError:
            break

        if _has_build_file(current):
            rel_str = rel.as_posix()
            if rel_str in {".", ""}:
                return "//..."
            return f"//{rel_str}/..."

        if current.resolve() == root_resolved:
            break
        current = current.parent

    return None


def _file_exists(path: Path) -> bool:
    """Return True if path is a regular file or exists without being a directory."""
    return path.is_file() or (path.exists() and not path.is_dir())


def _has_build_file(dir_path: Path) -> bool:
    """Return True if dir_path contains a BUILD or BUILD.bazel manifest."""
    return (
        (dir_path / "BUILD.bazel").is_file()
        or (dir_path / "BUILD.bazel").exists()
        or (dir_path / "BUILD").is_file()
        or (dir_path / "BUILD").exists()
    )


def _is_core_file(repo_root: Path, file_str: str) -> bool:
    """Check if the given file path refers to a root invalidation file."""
    path = Path(file_str)
    if path.is_absolute():
        try:
            rel = path.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            try:
                rel = path.relative_to(repo_root).as_posix()
            except ValueError:
                return False
    else:
        rel = file_str.removeprefix("./")
    return rel in CORE_FILES
