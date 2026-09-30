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


def validate_repository(repository_root: Path) -> None:
    """Validate the current physical Git-owned source tree."""

    validate_directories(source_directories(git_source_files(repository_root)))


def _enter_workspace() -> None:
    """bazel run starts in the runfiles tree; these checks read the worktree."""
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        os.chdir(workspace)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="repository root (defaults to Git's root)")
    args = parser.parse_args()
    try:
        repository_root = args.root or Path(
            subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        )
        validate_repository(repository_root)
    except (OSError, subprocess.CalledProcessError, SourceNameError):
        return 1
    return 0


if __name__ == "__main__":
    _enter_workspace()
    raise SystemExit(main())
