"""Analyze repository changes to determine affected Bazel targets, tests, and packages."""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

CORE_FILES: frozenset[str] = frozenset({
    ".bazelrc",
    ".bazelversion",
    "src/bazel/profiles/profiles.bazelrc",
})

# Matches the rule kinds that `kind('.*_test', ...)` selects, such as `py_test rule`.
_TEST_KIND = re.compile(r".*_test")


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

    # One query returns the changed files and everything that depends on them.
    # The changed files are its only source files, since nothing else in a
    # reverse-dependency closure can be a source file.
    file_set = " ".join(f'"{f}"' for f in valid_files)
    labelled_kinds = run_query(
        repo_root,
        f"rdeps(//..., set({file_set}), {max_rdeps_depth})",
        bazel_output_root=bazel_output_root,
        output="label_kind",
    )
    kinds = dict(_parse_label_kind(line) for line in labelled_kinds)
    direct_targets = sorted(label for label, kind in kinds.items() if kind == "source file")
    affected_targets = sorted(kinds)
    affected_tests = sorted(label for label, kind in kinds.items() if _TEST_KIND.search(kind))

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


class BazelQueryError(RuntimeError):
    """Raised when `bazel query` cannot run or fails without a partial result."""


def run_bazel_query(
    repo_root: Path,
    query_expr: str,
    bazel_output_root: str | None = None,
    output: str = "label",
) -> list[str]:
    """Execute bazel query with standard flags and return its result lines.

    Exit code 3 means --keep_going skipped some targets; the partial result is
    returned and Bazel's errors are echoed to stderr. Any other failure raises
    BazelQueryError, so a broken query never reads as "nothing affected".
    """
    cmd = ["bazel"]
    output_root = bazel_output_root or os.environ.get("BAZEL_OUTPUT_ROOT")
    if output_root:
        cmd.append(f"--output_user_root={output_root}")
    # Bazel emits query results as stdout events, so the filter keeps them.
    cmd.extend([
        "query",
        "--keep_going",
        "--ui_event_filters=-info",
        "--noshow_progress",
        f"--output={output}",
    ])
    # A query file keeps large target sets clear of the OS argument size limit.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".query") as query_file:
        query_file.write(query_expr)
        query_file.flush()
        cmd.append(f"--query_file={query_file.name}")
        try:
            res = subprocess.run(
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            msg = f"cannot run bazel query: {exc}"
            raise BazelQueryError(msg) from exc
    if res.returncode not in {0, 3}:
        msg = f"bazel query {query_expr!r} exited with {res.returncode}:\n{res.stderr.strip()}"
        raise BazelQueryError(msg)
    if res.returncode == 3:
        print(f"bazel query {query_expr!r} skipped targets:\n{res.stderr.strip()}", file=sys.stderr)
    return [
        line.strip()
        for line in res.stdout.splitlines()
        if line.strip() and line.split()[-1].startswith(("//", "@"))
    ]


def _parse_label_kind(line: str) -> tuple[str, str]:
    """Split a `--output=label_kind` line, such as `py_test rule //a:b`, into label and kind."""
    kind, _, label = line.rpartition(" ")
    return label, kind


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
