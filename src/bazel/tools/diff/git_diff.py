"""Extracts Git baseline refs, changed file lists, and structured diff results."""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


@dataclasses.dataclass(frozen=True)
class FileDiff:
    """Represents differences and patch information for a single file."""

    path: str
    old_path: str | None = None
    status: str = "M"
    additions: int = 0
    deletions: int = 0
    patch: str = ""


@dataclasses.dataclass(frozen=True)
class GitDiffResult:
    """Aggregated git diff result across multiple files."""

    base_ref: str
    target_ref: str
    files: list[FileDiff]
    total_additions: int
    total_deletions: int
    raw_patch: str

    @property
    def changed_paths(self) -> list[str]:
        """Return the list of changed file paths."""
        return [f.path for f in self.files]


def _run_git(repo_root: Path, args: Sequence[str]) -> str:
    """Run a git command and return stripped stdout or empty string on error."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


# LINT.IfChange(ci_push_to_main)
def is_ci_push_to_main(env: Mapping[str, str] | None = None) -> bool:
    """Detects a CI push run on main.

    BuildBuddy Workflows export CI=true, GIT_BRANCH, and GIT_PR_NUMBER (0 for
    push triggers). GitHub Actions exports GITHUB_ACTIONS=true,
    GITHUB_EVENT_NAME, and GITHUB_REF_NAME.
    """
    env = os.environ if env is None else env
    buildbuddy_push = (
        env.get("CI") == "true"
        and env.get("GIT_BRANCH") == "main"
        and env.get("GIT_PR_NUMBER", "0") in {"", "0"}
    )
    github_push = (
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_EVENT_NAME") == "push"
        and env.get("GITHUB_REF_NAME") == "main"
    )
    return buildbuddy_push or github_push


# LINT.ThenChange(//src/bazel/rules/lint_aspect/format.bzl:ci_push_to_main)

CI_PUSH_BASE_ENV_VARS: Sequence[str] = (
    "BEFORE_COMMIT",
    "GIT_PREVIOUS_COMMIT",
    "GIT_BEFORE",
    "BEFORE",
    "GITHUB_BEFORE",
)


def detect_git_baseline(
    repo_root: Path,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Finds merge-base against origin/main (or main), falling back to HEAD~1 or HEAD.

    On CI push runs to main, diffs against the replaced commit obtained from CI
    environment variables or HEAD~1. If that commit does not exist in the
    repository (e.g. after a force-push), falls back to an empty baseline string
    to signal full/all mode.
    """
    active_env = os.environ if env is None else env
    if is_ci_push_to_main(active_env):
        candidate = ""
        for var in CI_PUSH_BASE_ENV_VARS:
            val = active_env.get(var)
            if val:
                candidate = val.strip()
                break
        if not candidate:
            candidate = "HEAD~1"

        verified = _run_git(repo_root, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
        if verified:
            return verified, "HEAD"
        return "", "HEAD"

    head_sha = _run_git(repo_root, ["rev-parse", "HEAD"])
    for candidate in ("origin/main", "main"):
        base = _run_git(repo_root, ["merge-base", "HEAD", candidate])
        if base and base != head_sha:
            return base, "HEAD"

    if _run_git(repo_root, ["rev-parse", "--verify", "HEAD~1"]):
        return "HEAD~1", "HEAD"

    return "HEAD", "HEAD"


def get_changed_files(repo_root: Path, base_ref: str | None = None) -> list[str]:
    """Computes all changed files across branch commits, staged, unstaged, and untracked."""
    changed: set[str] = set()

    if base_ref is not None:
        base = base_ref
    else:
        resolved_base, _ = detect_git_baseline(repo_root)
        base = resolved_base or "HEAD"

    # 1. Diff against base (covers branch commits, staged, and unstaged)
    diff_output = _run_git(repo_root, ["diff", "--name-only", base])
    if diff_output:
        changed.update(
            raw_line.strip() for raw_line in diff_output.splitlines() if raw_line.strip()
        )

    # 2. Staged: git diff --name-only --cached
    staged = _run_git(repo_root, ["diff", "--name-only", "--cached"])
    if staged:
        changed.update(raw_line.strip() for raw_line in staged.splitlines() if raw_line.strip())

    # 3. Untracked: git ls-files --others --exclude-standard
    untracked = _run_git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    if untracked:
        changed.update(raw_line.strip() for raw_line in untracked.splitlines() if raw_line.strip())

    return sorted(changed)


def parse_numstat(output: str) -> dict[str, tuple[int, int, str | None]]:
    """Parse output of git diff --numstat."""
    stats: dict[str, tuple[int, int, str | None]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_str, deleted_str, path_str = parts[0], parts[1], parts[2]
        added = int(added_str) if added_str.isdigit() else 0
        deleted = int(deleted_str) if deleted_str.isdigit() else 0
        old_path = None
        # Handle renames like {old => new}/file or old => new
        if " => " in path_str:
            match = re.match(r"(?:(.*)\{)?([^{}]+) => ([^{}]+)(?:\}(.*))?", path_str)
            if match:
                prefix = match.group(1) or ""
                old_sub = match.group(2)
                new_sub = match.group(3)
                suffix = match.group(4) or ""
                old_path = f"{prefix}{old_sub}{suffix}".replace("//", "/")
                new_path = f"{prefix}{new_sub}{suffix}".replace("//", "/")
                path_str = new_path
        stats[path_str.strip('"')] = (added, deleted, old_path)
    return stats


def parse_name_status(output: str) -> dict[str, tuple[str, str | None]]:
    """Parse output of git diff --name-status."""
    status_map: dict[str, tuple[str, str | None]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        status_code = parts[0]
        if status_code.startswith("R") and len(parts) >= 3:
            old_path = parts[1].strip('"')
            new_path = parts[2].strip('"')
            status_map[new_path] = (status_code[0], old_path)
        elif len(parts) >= 2:
            status_map[parts[1].strip('"')] = (status_code[0], None)
    return status_map


def split_patch_by_file(raw_patch: str) -> dict[str, str]:
    """Splits a multi-file unified patch into per-file patches keyed by target path."""
    file_patches: dict[str, list[str]] = {}
    current_file: str | None = None
    current_lines: list[str] = []

    for line in raw_patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_file and current_lines:
                file_patches[current_file] = current_lines
            m = re.search(r' b/(?:"([^"]+)"|(.+))$', line.strip())
            if m:
                g1 = m.group(1)
                g2 = m.group(2)
                raw_path = str(g1 if g1 is not None else (g2 if g2 is not None else ""))
                current_file = raw_path.strip().strip('"')
            else:
                current_file = None
            current_lines = [line]
        elif current_file:
            current_lines.append(line)

    if current_file and current_lines:
        file_patches[current_file] = current_lines

    return {k: "".join(v) for k, v in file_patches.items()}


def get_git_diff(
    repo_root: Path,
    base_ref: str | None = None,
    target_ref: str | None = None,
) -> GitDiffResult:
    """Runs git diff --numstat and git diff -p across base/target range and returns GitDiffResult."""
    if base_ref is None and target_ref is None:
        resolved_base, resolved_target = detect_git_baseline(repo_root)
    elif base_ref is None:
        resolved_base, _ = detect_git_baseline(repo_root)
        resolved_target = target_ref or "HEAD"
    else:
        resolved_base = base_ref
        resolved_target = target_ref or "HEAD"

    if not resolved_base or resolved_base == resolved_target:
        return GitDiffResult(
            base_ref=resolved_base,
            target_ref=resolved_target,
            files=[],
            total_additions=0,
            total_deletions=0,
            raw_patch="",
        )

    range_spec = f"{resolved_base}...{resolved_target}"
    numstat_out = _run_git(repo_root, ["diff", "--numstat", range_spec])
    status_out = _run_git(repo_root, ["diff", "--name-status", range_spec])
    raw_patch = _run_git(repo_root, ["diff", "-p", range_spec])

    if not numstat_out and not status_out and not raw_patch:
        numstat_out = _run_git(repo_root, ["diff", "--numstat", resolved_base, resolved_target])
        status_out = _run_git(repo_root, ["diff", "--name-status", resolved_base, resolved_target])
        raw_patch = _run_git(repo_root, ["diff", "-p", resolved_base, resolved_target])

    numstats = parse_numstat(numstat_out)
    statuses = parse_name_status(status_out)
    file_patches = split_patch_by_file(raw_patch)

    all_paths = sorted(set(numstats.keys()) | set(statuses.keys()) | set(file_patches.keys()))
    files: list[FileDiff] = []
    total_add = 0
    total_del = 0

    for path in all_paths:
        added, deleted, stat_old = numstats.get(path, (0, 0, None))
        status_code, status_old = statuses.get(path, ("M", None))
        old_path = status_old or stat_old
        patch = file_patches.get(path, "")

        files.append(
            FileDiff(
                path=path,
                old_path=old_path,
                status=status_code,
                additions=added,
                deletions=deleted,
                patch=patch,
            )
        )
        total_add += added
        total_del += deleted

    return GitDiffResult(
        base_ref=resolved_base,
        target_ref=resolved_target,
        files=files,
        total_additions=total_add,
        total_deletions=total_del,
        raw_patch=raw_patch,
    )
