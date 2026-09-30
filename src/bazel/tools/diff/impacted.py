"""Bazel-diff impacted target analysis with hash caching and worktree isolation."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from src.bazel.tools.diff.hash_cache import fetch, store
except ImportError:  # pragma: no cover
    from hash_cache import fetch, store  # type: ignore[no-redef]

if TYPE_CHECKING:
    from collections.abc import Sequence


class BazelDiffError(RuntimeError):
    """Raised when bazel-diff computation or prerequisite git operations fail."""


@dataclasses.dataclass(frozen=True)
class ImpactedTarget:
    """An impacted Bazel target with distance information from changed roots."""

    label: str
    target_distance: int
    package_distance: int


@dataclasses.dataclass(frozen=True)
class ImpactedResult:
    """Impacted targets categorized into direct and transitive sets."""

    all_targets: list[str]
    direct_targets: list[str]
    targets: list[ImpactedTarget]


_BAZEL_DIFF_VERSION_CACHE: dict[str, str] = {}


def get_repo_cache_dir(repo_cache_dir: str | Path | None = None) -> Path:
    """Return the repository cache directory from argument, environment, or default."""
    if repo_cache_dir is not None:
        return Path(repo_cache_dir)
    env_cache = os.environ.get("REPO_CACHE_DIR")
    if env_cache:
        return Path(env_cache)
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
    return base / "repo"


def resolve_bazel_diff_cmd(
    bazel_diff_bin: str | Path | None = None,
    bazel_output_root: str | None = None,
) -> list[str]:
    """Resolve command prefix used to invoke bazel-diff."""
    if bazel_diff_bin:
        return [str(bazel_diff_bin)]
    env_bin = os.environ.get("BAZEL_DIFF_BIN")
    if env_bin:
        return [env_bin]
    which_bin = shutil.which("bazel-diff")
    if which_bin:
        return [which_bin]

    cmd = ["bazel"]
    output_root = bazel_output_root or os.environ.get("BAZEL_OUTPUT_ROOT")
    if output_root:
        cmd.append(f"--output_user_root={output_root}")
    cmd.extend([
        "run",
        "--ui_event_filters=-info,-stdout",
        "--noshow_progress",
        "//src/bazel/tools:bazel-diff",
        "--",
    ])
    return cmd


def get_bazel_diff_version(bazel_diff_cmd: Sequence[str] | None = None) -> str:
    """Return version string of the bazel-diff binary, caching the result."""
    cmd = list(bazel_diff_cmd or resolve_bazel_diff_cmd())
    cmd_key = " ".join(cmd)
    if cmd_key in _BAZEL_DIFF_VERSION_CACHE:
        return _BAZEL_DIFF_VERSION_CACHE[cmd_key]

    version = "49.1.0"
    try:
        proc = subprocess.run(
            [*cmd, "-V"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            version = proc.stdout.strip()
    except OSError:
        pass

    _BAZEL_DIFF_VERSION_CACHE[cmd_key] = version
    return version


def canonical_hashing_flags() -> str:
    """Return canonical hashing flags string."""
    return "standard"


def compute_cache_key(
    commit_sha: str,
    bazel_diff_version: str,
    hashing_flags: str = "",
) -> str:
    """Derive deterministic remote cache key from commit SHA, tool version, and flags."""
    parts = ["bazel-diff", commit_sha, bazel_diff_version]
    if hashing_flags:
        parts.append(hashing_flags)
    return ":".join(parts)


def is_working_tree_clean(repo_root: Path) -> bool:
    """Return True if working tree has no uncommitted changes."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0 and not bool(proc.stdout.strip())
    except OSError:
        return False


def parse_impacted_targets(raw_output: str) -> ImpactedResult:
    """Parse bazel-diff get-impacted-targets JSON or newline-separated output."""
    stripped = raw_output.strip()
    if not stripped:
        return ImpactedResult(all_targets=[], direct_targets=[], targets=[])

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        labels = [
            line.strip()
            for line in stripped.splitlines()
            if line.strip() and line.strip().startswith(("//", "@"))
        ]
        sorted_labels = sorted(set(labels))
        return ImpactedResult(
            all_targets=sorted_labels,
            direct_targets=sorted_labels,
            targets=[
                ImpactedTarget(label=lbl, target_distance=0, package_distance=0)
                for lbl in sorted_labels
            ],
        )

    if not isinstance(data, list):
        msg = f"unexpected bazel-diff output structure: expected list, got {type(data).__name__}"
        raise BazelDiffError(msg)

    parsed_targets: list[ImpactedTarget] = []
    direct_labels: set[str] = set()
    all_labels: set[str] = set()

    for item in data:
        if isinstance(item, str):
            label = item.strip()
            if label:
                all_labels.add(label)
                direct_labels.add(label)
                parsed_targets.append(
                    ImpactedTarget(label=label, target_distance=0, package_distance=0)
                )
            continue

        if not isinstance(item, dict):
            continue

        label = str(item.get("label") or item.get("target") or "").strip()
        if not label:
            continue

        target_dist = int(item.get("targetDistance", item.get("target_distance", 0)))
        package_dist = int(item.get("packageDistance", item.get("package_distance", 0)))

        parsed_targets.append(
            ImpactedTarget(
                label=label,
                target_distance=target_dist,
                package_distance=package_dist,
            )
        )
        all_labels.add(label)
        if target_dist == 0:
            direct_labels.add(label)

    return ImpactedResult(
        all_targets=sorted(all_labels),
        direct_targets=sorted(direct_labels),
        targets=parsed_targets,
    )


def _run_cmd(cmd: Sequence[str], cwd: Path | None = None) -> str:
    """Run a subprocess command and return stdout or raise BazelDiffError on failure."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        msg = f"failed to execute command {cmd!r}: {exc}"
        raise BazelDiffError(msg) from exc

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        msg = f"command {cmd!r} failed with exit code {proc.returncode}: {err}"
        raise BazelDiffError(msg)
    return proc.stdout.strip()


def resolve_commit_sha(repo_root: Path, rev: str) -> str:
    """Resolve a git revision into a 40-character commit SHA."""
    out = _run_cmd(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=repo_root)
    sha = out.strip()
    if len(sha) != 40:
        msg = f"invalid commit SHA resolved for revision {rev!r}: {sha!r}"
        raise BazelDiffError(msg)
    return sha


def generate_hashes(
    workspace_path: Path,
    output_path: Path,
    *,
    dep_edges_path: Path | None = None,
    bazel_diff_cmd: Sequence[str] | None = None,
    bazel_output_root: str | None = None,
) -> None:
    """Run bazel-diff generate-hashes for a given workspace path."""
    cmd = list(bazel_diff_cmd or resolve_bazel_diff_cmd(bazel_output_root=bazel_output_root))
    cmd.extend(["generate-hashes", "-w", str(workspace_path.resolve())])

    output_root = bazel_output_root or os.environ.get("BAZEL_OUTPUT_ROOT")
    if output_root:
        cmd.extend(["--bazelStartupOptions", f"--output_user_root={output_root}"])

    if dep_edges_path is not None:
        cmd.extend(["--depEdgesFile", str(dep_edges_path.resolve())])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd.append(str(output_path.resolve()))
    _run_cmd(cmd, cwd=workspace_path)


def ensure_commit_hashes(
    repo_root: Path,
    commit_sha: str,
    *,
    is_head: bool,
    tree_clean: bool,
    cache_dir: Path,
    diff_cmd: Sequence[str],
    diff_version: str,
    hashing_flags: str,
    bazel_output_root: str | None,
) -> Path:
    """Ensure hashes exist for a commit via local cache, remote cache, or generation."""
    local_hash_path = cache_dir / f"{commit_sha}.json"
    cache_key = compute_cache_key(commit_sha, diff_version, hashing_flags)

    # 1. Local cache lookup
    if local_hash_path.exists() and local_hash_path.stat().st_size > 0:
        print(
            f"[INFO] Using locally cached bazel-diff hashes for commit {commit_sha[:8]}",
            file=sys.stderr,
        )
        return local_hash_path

    # 2. Remote cache lookup
    remote_data = fetch(cache_key, repo_root=repo_root)
    if remote_data is not None:
        print(
            f"[INFO] Downloaded bazel-diff hashes from remote cache for commit {commit_sha[:8]}",
            file=sys.stderr,
        )
        local_hash_path.write_bytes(remote_data)
        return local_hash_path

    # 3. Generate hashes on miss
    print(f"[INFO] Generating bazel-diff hashes for commit {commit_sha[:8]}", file=sys.stderr)
    if is_head:
        if tree_clean:
            temp_hash = local_hash_path.with_suffix(".tmp")
            generate_hashes(
                repo_root, temp_hash, bazel_diff_cmd=diff_cmd, bazel_output_root=bazel_output_root
            )
            if temp_hash.exists():
                temp_hash.replace(local_hash_path)
            else:
                local_hash_path.touch()
            store(cache_key, local_hash_path.read_bytes(), repo_root=repo_root)
            print(
                f"[INFO] Uploaded bazel-diff hashes to remote cache for commit {commit_sha[:8]}",
                file=sys.stderr,
            )
            return local_hash_path

        temp_dir = tempfile.mkdtemp(prefix="bazel_diff_dirty_")
        dirty_hash = Path(temp_dir) / "head_hashes.json"
        generate_hashes(
            repo_root, dirty_hash, bazel_diff_cmd=diff_cmd, bazel_output_root=bazel_output_root
        )
        return dirty_hash

    # Base commit miss: worktree fallback sharing bazel_output_root
    temp_wt_parent = tempfile.mkdtemp(prefix="bazel_diff_base_")
    temp_wt_path = Path(temp_wt_parent) / "worktree"
    try:
        _run_cmd(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "worktree",
                "add",
                "--detach",
                str(temp_wt_path),
                commit_sha,
            ],
            cwd=repo_root,
        )
        temp_hash = local_hash_path.with_suffix(".tmp")
        generate_hashes(
            temp_wt_path, temp_hash, bazel_diff_cmd=diff_cmd, bazel_output_root=bazel_output_root
        )
        if temp_hash.exists():
            temp_hash.replace(local_hash_path)
        else:
            local_hash_path.touch()
        store(cache_key, local_hash_path.read_bytes(), repo_root=repo_root)
        print(
            f"[INFO] Uploaded bazel-diff hashes to remote cache for commit {commit_sha[:8]}",
            file=sys.stderr,
        )
        return local_hash_path
    finally:
        _run_cmd(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "worktree",
                "remove",
                "--force",
                str(temp_wt_path),
            ],
            cwd=repo_root,
        )
        shutil.rmtree(temp_wt_parent, ignore_errors=True)


def compute_impacted_targets(
    workspace_path: Path,
    starting_hashes_path: Path,
    final_hashes_path: Path,
    *,
    bazel_diff_cmd: Sequence[str] | None = None,
    bazel_output_root: str | None = None,
) -> ImpactedResult:
    """Run bazel-diff get-impacted-targets and parse the result."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp_out:
        out_path = Path(tmp_out.name)
    try:
        cmd = list(bazel_diff_cmd or resolve_bazel_diff_cmd(bazel_output_root=bazel_output_root))
        cmd.extend([
            "get-impacted-targets",
            "-sh",
            str(starting_hashes_path.resolve()),
            "-fh",
            str(final_hashes_path.resolve()),
            "-w",
            str(workspace_path.resolve()),
            "-o",
            str(out_path.resolve()),
        ])
        _run_cmd(cmd, cwd=workspace_path)
        content = out_path.read_text(encoding="utf-8")
        return parse_impacted_targets(content)
    finally:
        out_path.unlink(missing_ok=True)


def get_impacted_targets(
    repo_root: Path,
    base_commit: str,
    *,
    repo_cache_dir: str | Path | None = None,
    bazel_diff_bin: str | Path | None = None,
    bazel_output_root: str | None = None,
) -> ImpactedResult:
    """Compute impacted targets between base commit and working tree using bazel-diff."""
    base_sha = resolve_commit_sha(repo_root, base_commit)
    head_sha = resolve_commit_sha(repo_root, "HEAD")
    cache_root = get_repo_cache_dir(repo_cache_dir)
    diff_dir = cache_root / "bazel-diff"
    diff_dir.mkdir(parents=True, exist_ok=True)

    diff_cmd = resolve_bazel_diff_cmd(
        bazel_diff_bin=bazel_diff_bin,
        bazel_output_root=bazel_output_root,
    )
    diff_version = get_bazel_diff_version(diff_cmd)
    hashing_flags = canonical_hashing_flags()

    base_hash_path = ensure_commit_hashes(
        repo_root=repo_root,
        commit_sha=base_sha,
        is_head=False,
        tree_clean=True,
        cache_dir=diff_dir,
        diff_cmd=diff_cmd,
        diff_version=diff_version,
        hashing_flags=hashing_flags,
        bazel_output_root=bazel_output_root,
    )

    tree_clean = is_working_tree_clean(repo_root)
    head_hash_path = ensure_commit_hashes(
        repo_root=repo_root,
        commit_sha=head_sha,
        is_head=True,
        tree_clean=tree_clean,
        cache_dir=diff_dir,
        diff_cmd=diff_cmd,
        diff_version=diff_version,
        hashing_flags=hashing_flags,
        bazel_output_root=bazel_output_root,
    )

    return compute_impacted_targets(
        repo_root,
        starting_hashes_path=base_hash_path,
        final_hashes_path=head_hash_path,
        bazel_diff_cmd=diff_cmd,
        bazel_output_root=bazel_output_root,
    )
