#!/usr/bin/env python3
"""Validate source directory naming conventions to guarantee collision-free, DNS-compliant Kubernetes resource names."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

SOURCE_ROOT = PurePosixPath("src")
SNAKE_CASE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Terraform's conventional private-module namespaces are not projected to a
# Kubernetes resource identifier.
NON_PROJECTED_DIRECTORIES = {"_modules", "_interface"}


class SourceNameError(ValueError):
    """The source inventory contains a name that cannot be projected safely."""


def git_source_files(
    repository_root: Path, source_root: PurePosixPath = SOURCE_ROOT
) -> list[PurePosixPath]:
    """Return physical, nonignored Git files below the source root."""

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    files: list[PurePosixPath] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = PurePosixPath(raw_path.decode("utf-8"))
        if (
            relative == source_root
            or source_root not in relative.parents
            or (len(relative.parts) > 1 and relative.parts[1] == "third_party")
        ):
            continue
        if (repository_root / Path(*relative.parts)).is_file():
            files.append(relative)
    return files


def git_changed_source_files(
    repository_root: Path,
    source_root: PurePosixPath = SOURCE_ROOT,
    changed_files: Iterable[str | PurePosixPath] | None = None,
) -> list[PurePosixPath]:
    """Return physical, nonignored changed Git files below the source root."""

    if changed_files is not None:
        raw_paths = [str(p).strip() for p in changed_files if str(p).strip()]
    elif "CHANGED_ALL" in os.environ:
        raw_paths = [p.strip() for p in os.environ["CHANGED_ALL"].splitlines() if p.strip()]
    else:
        try:
            diff_base = subprocess.check_output(
                ["git", "-C", str(repository_root), "merge-base", "HEAD", "origin/main"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, OSError):
            try:
                diff_base = subprocess.check_output(
                    ["git", "-C", str(repository_root), "merge-base", "HEAD", "main"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except (subprocess.CalledProcessError, OSError):
                diff_base = "HEAD"
        try:
            diff_out = subprocess.check_output(
                ["git", "-C", str(repository_root), "diff", "--name-only", diff_base],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            diff_out = ""
        try:
            untracked_out = subprocess.check_output(
                ["git", "-C", str(repository_root), "ls-files", "--others", "--exclude-standard"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            untracked_out = ""
        raw_paths = [p.strip() for p in (diff_out + "\n" + untracked_out).splitlines() if p.strip()]

    files: list[PurePosixPath] = []
    for raw_path in raw_paths:
        relative = PurePosixPath(raw_path)
        if (
            relative == source_root
            or source_root not in relative.parents
            or (len(relative.parts) > 1 and relative.parts[1] == "third_party")
        ):
            continue
        if (repository_root / Path(*relative.parts)).is_file():
            files.append(relative)
    return files


def source_directories(files: Iterable[PurePosixPath]) -> set[tuple[str, ...]]:
    """Collect every physical source directory represented by an inventory file."""

    directories: set[tuple[str, ...]] = set()
    for file_path in files:
        relative = file_path.relative_to(SOURCE_ROOT)
        directories.update(relative.parts[:depth] for depth in range(1, len(relative.parts)))
    return directories


def validate_directories(
    directories: Iterable[tuple[str, ...]],
    *,
    source_root: PurePosixPath = SOURCE_ROOT,
) -> None:
    """Validate snake_case segments, DNS projections, and projection uniqueness."""

    errors: list[str] = []
    projections: defaultdict[str, set[str]] = defaultdict(set)
    paths_by_projection: defaultdict[str, list[str]] = defaultdict(list)

    for parts in sorted(set(directories)):
        path = "/".join((source_root.name, *parts))
        name = parts[-1]
        if name in NON_PROJECTED_DIRECTORIES:
            continue
        if not SNAKE_CASE.fullmatch(name):
            errors.append(f"{path}: directory name must use snake_case")

        projection = name.replace("_", "-")
        projections[projection].add(name)
        paths_by_projection[projection].append(path)
        if not DNS_LABEL.fullmatch(projection):
            errors.append(f"{path}: derived DNS name {projection!r} is not DNS-1123-safe")

    for projection, names in sorted(projections.items()):
        if len(names) > 1:
            paths = ", ".join(sorted(paths_by_projection[projection]))
            errors.append(
                f"derived DNS name {projection!r} is not injective for distinct directories: {paths}"
            )

    if errors:
        raise SourceNameError("\n".join(errors))


def validate_repository(
    repository_root: Path,
    *,
    check_mode: str | None = None,
    changed_files: Iterable[str | PurePosixPath] | None = None,
) -> None:
    """Validate the current physical Git-owned source tree."""

    mode = check_mode if check_mode is not None else os.environ.get("CHECK_MODE", "all")
    if mode == "affected":
        files = git_changed_source_files(repository_root, changed_files=changed_files)
        if not files:
            return
        directories = source_directories(files)
        if not directories:
            return
        validate_directories(directories)
        return

    validate_directories(source_directories(git_source_files(repository_root)))


def _enter_workspace() -> None:
    """bazel run starts in the runfiles tree; these checks read the worktree."""
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        os.chdir(workspace)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="repository root (defaults to Git's root)")
    parser.add_argument(
        "--mode",
        choices=["affected", "all"],
        default=None,
        help="validation mode (defaults to CHECK_MODE env var or 'all')",
    )
    args = parser.parse_args()
    try:
        repository_root = args.root or Path(
            subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        )
        validate_repository(repository_root, check_mode=args.mode)
    except (OSError, subprocess.CalledProcessError, SourceNameError):
        return 1
    return 0


if __name__ == "__main__":
    _enter_workspace()
    raise SystemExit(main())
