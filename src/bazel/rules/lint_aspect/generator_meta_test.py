"""Tests for fix generator metadata and Gazelle target directory computation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.bazel.rules.lint_aspect import generator_meta


def test_generator_meta_mode_all() -> None:
    active, dirs = generator_meta.get_generator_metadata(["package.json"], mode="all")
    assert "pnpm" in active
    assert "gazelle" in active
    assert dirs == []


def test_generator_meta_mode_affected_pnpm() -> None:
    active, dirs = generator_meta.get_generator_metadata(["package.json"], mode="affected")
    assert active == ["pnpm"]
    assert dirs == []


def test_generator_meta_mode_affected_uv() -> None:
    active, dirs = generator_meta.get_generator_metadata(["pyproject.toml"], mode="affected")
    assert active == ["uv"]
    assert dirs == []


def test_compute_gazelle_dirs_nested() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "BUILD.bazel").touch()
        pkg = root / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "BUILD.bazel").touch()
        subpkg = pkg / "sub"
        subpkg.mkdir(parents=True)
        (subpkg / "BUILD.bazel").touch()

        # File in subpkg should resolve to src/pkg/sub
        assert generator_meta.find_package_dir("src/pkg/sub/test.py", root) == "src/pkg/sub"
        # File in pkg should resolve to src/pkg
        assert generator_meta.find_package_dir("src/pkg/other.py", root) == "src/pkg"

        # compute_gazelle_dirs minimizes redundant child dirs
        dirs = generator_meta.compute_gazelle_dirs(
            ["src/pkg/sub/test.py", "src/pkg/other.py"], root
        )
        assert dirs == ["src/pkg"]


def test_compute_gazelle_dirs_root() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "BUILD.bazel").touch()
        dirs = generator_meta.compute_gazelle_dirs(["BUILD.bazel", "src/file.py"], root)
        assert dirs == ["."]
