"""Determine active fix generators and affected Gazelle directories from changed files."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

# Ensure repo root is on sys.path so we can import gate_diff
REPO_ROOT = Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from src.bazel.tools.diff import gate_diff

    GENERATOR_TRIGGERS = gate_diff.GENERATOR_TRIGGERS
    matches_pattern = gate_diff.matches_pattern
except Exception:
    import fnmatch

    def matches_pattern(pattern: str, filepath: str) -> bool:
        filepath = filepath.strip()
        if not filepath:
            return False
        posix_path = PurePosixPath(filepath)
        normalized = posix_path.as_posix()
        filename = posix_path.name
        if pattern == "*":
            return True
        if pattern == "**/templates/**":
            return "templates" in posix_path.parts[:-1]
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            return normalized == prefix or normalized.startswith(prefix + "/")
        if "/" in pattern:
            return normalized == pattern or fnmatch.fnmatch(normalized, pattern)
        return fnmatch.fnmatch(filename, pattern)

    GENERATOR_TRIGGERS = {
        "pnpm": ["package.json"],
        "uv": ["pyproject.toml"],
        "team_records": [
            ".github/CODEOWNERS",
            "CODEOWNERS",
            "deployment/project.yaml",
            "src/infra/definitions/teams/**",
            "src/infra/terraform/deployments/**/deployment.yaml",
        ],
        "rulesync": ["src/bazel/checks/rulesync/**", "*.rulesync.md"],
        "gazelle": [
            "BUILD.bazel",
            "BUILD",
            "*.py",
            "*.tf",
            "*.tfvars",
            "Chart.yaml",
            "kustomization.yaml",
            "MODULE.bazel",
            "*.go",
        ],
        "artwork": ["src/infra/docs/artwork/**"],
    }


def find_package_dir(filepath: str, repo_root: Path) -> str:
    """Find the closest enclosing package directory containing a BUILD or BUILD.bazel file."""
    path = repo_root / filepath
    curr = path.parent
    while curr != repo_root and repo_root in curr.parents:
        if (curr / "BUILD.bazel").is_file() or (curr / "BUILD").is_file():
            return curr.relative_to(repo_root).as_posix()
        curr = curr.parent
    return "."


def compute_gazelle_dirs(matched_files: list[str], repo_root: Path) -> list[str]:
    """Compute minimal non-overlapping package directories for Gazelle execution."""
    raw_dirs = {find_package_dir(f, repo_root) for f in matched_files}
    if "." in raw_dirs:
        return ["."]
    sorted_dirs = sorted(raw_dirs, key=lambda d: (d.count("/"), len(d)))
    min_dirs: list[str] = []
    for d in sorted_dirs:
        if not any(d == parent or d.startswith(parent + "/") for parent in min_dirs):
            min_dirs.append(d)
    return min_dirs


def get_generator_metadata(
    changed_files: list[str],
    mode: str,
    repo_root: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Return active generator names and minimal Gazelle directories."""
    root = repo_root or Path.cwd()
    if mode == "all":
        return list(GENERATOR_TRIGGERS.keys()), []

    active: list[str] = []
    gazelle_dirs: list[str] = []
    for gen_name, patterns in GENERATOR_TRIGGERS.items():
        matched = [f for f in changed_files if any(matches_pattern(pat, f) for pat in patterns)]
        if matched:
            active.append(gen_name)
            if gen_name == "gazelle":
                gazelle_dirs = compute_gazelle_dirs(matched, root)

    return active, gazelle_dirs


def main() -> None:
    raw_changed = os.environ.get("CHANGED_ALL", "")
    mode = os.environ.get("FIX_MODE", "affected")
    changed_files = [line.strip() for line in raw_changed.splitlines() if line.strip()]

    active, gazelle_dirs = get_generator_metadata(changed_files, mode)
    print(" ".join(active))
    for d in gazelle_dirs:
        print(d)


if __name__ == "__main__":
    main()
