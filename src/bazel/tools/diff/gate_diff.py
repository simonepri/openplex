"""Filter static analysis gates and fix generators based on changed workspace files.

Maps file modifications to Bazel gate targets and generators with core file drift invalidation.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

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

GATE_TRIGGERS: dict[str, list[str]] = {
    "//src/bazel/checks:unclaimed_files": ["*"],
    "//src/bazel/checks/cluster_network": [
        "*.k8s.yaml",
        "src/infra/definitions/cluster/**",
        "src/infra/argocd/**",
    ],
    "//src/bazel/checks/records:codeowners": [
        "CODEOWNERS",
        "src/infra/definitions/teams/**",
    ],
    "//src/bazel/checks/source_names:source_names": ["*"],
    "//src/bazel/checks/records:team_records": [
        "src/infra/definitions/teams/**",
        "deployment/project.yaml",
    ],
    "//src/bazel/checks/workload_portability:workload_portability": [
        "*.k8s.yaml",
        "src/examples/**",
    ],
    "//src/bazel/checks/network_exposure:network_exposure": [
        "*.k8s.yaml",
        "src/infra/definitions/**",
    ],
    "//src/bazel/checks:opengrep": [
        "*.py",
        "*.go",
        "*.sh",
        "*.ts",
        "*.bzl",
        "*.yaml",
        "*.json",
    ],
    "//src/bazel/checks:dev_tool_isolation": [
        "Dockerfile*",
        "package.json",
        "pyproject.toml",
        "requirements*.txt",
        "multitool.lock.json",
        "src/bazel/checks/**",
    ],
    "//src/bazel/checks:dotenv": [
        ".env*",
        "*.env",
    ],
    "//src/bazel/checks:json_schemas": [
        "*.json",
        "*.schema.json",
    ],
    "//src/bazel/checks/argocd_links": [
        "src/infra/argocd/**",
        "*.k8s.yaml",
        "kustomization.yaml",
    ],
    "//src/bazel/checks:coder_templates": [
        "src/infra/definitions/workspaces/**",
    ],
    "//src/bazel/checks:license_images": [
        "src/infra/images/**",
        "Dockerfile*",
        "package.json",
        "requirements*.txt",
    ],
    "//src/bazel/checks:license_source": [
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements*.txt",
    ],
    "//src/bazel/checks:trivy_source": [
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements*.txt",
    ],
    "//src/bazel/checks:kubescape": [
        "*.k8s.yaml",
        "**/templates/**",
        "Chart.yaml",
        "kustomization.yaml",
    ],
    "//src/bazel/checks:ifttt": [
        "LINT.IfChange",
    ],
    "//src/bazel/checks/rulesync": [
        "*.rulesync.md",
        ".agents/rules/**",
        "src/bazel/checks/rulesync/**",
    ],
    "//src/bazel/checks/function_length": [
        "*.py",
        "*.bzl",
    ],
    "//src/bazel/checks:lock_drift": [
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements*.txt",
        "uv.lock",
    ],
    "//src/bazel/checks:dups": [
        "*.py",
        "*.go",
        "*.sh",
        "*.ts",
        "*.tsx",
        "*.js",
        "*.jsx",
        "*.tf",
        "*.yaml",
        "*.json",
    ],
    "//src/bazel/checks:cyclo": [
        "*.go",
    ],
}

GENERATOR_TRIGGERS: dict[str, list[str]] = {
    "pnpm": ["package.json"],
    "uv": ["pyproject.toml"],
    "team_records": [
        ".github/CODEOWNERS",
        "CODEOWNERS",
        "deployment/project.yaml",
        "src/infra/definitions/teams/**",
    ],
    "rulesync": ["src/bazel/checks/rulesync/**", "*.rulesync.md"],
    # Format fails when Gazelle rewrites a BUILD file, so these triggers also
    # decide when CI checks for Gazelle drift.
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
    "artwork": [
        "src/infra/docs/artwork/**",
    ],
}


def is_core_file(filepath: str) -> bool:
    """Return True if the filepath corresponds to a core workspace configuration file."""
    normalized = PurePosixPath(filepath).as_posix()
    return normalized in CORE_FILES


def file_contains_ifchange(filepath: str, repo_root: Path | None = None) -> bool:
    """Return True if the specified file contains the LINT.IfChange marker directive."""
    if "LINT.IfChange" in filepath:
        return True
    root = repo_root or Path.cwd()
    target = root / filepath
    if not target.is_file():
        return False
    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
        return "LINT.IfChange" in content
    except OSError:
        return False


def matches_pattern(pattern: str, filepath: str) -> bool:
    """Evaluate whether a relative file path matches a trigger glob pattern or path prefix."""
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


def normalize_gate_label(gate: str) -> str:
    """Normalize a Bazel gate label to its canonical entry in GATE_TRIGGERS if possible."""
    cleaned = gate.strip().lstrip("@")
    if not cleaned.startswith("//"):
        cleaned = "//" + cleaned.lstrip("/")

    candidates: list[str] = [cleaned]
    if cleaned.endswith("_check"):
        candidates.append(cleaned[:-6])

    pkg, colon, target = cleaned.partition(":")
    if colon:
        target_base = target.removesuffix("_check")
        candidates.extend([f"{pkg}:{target_base}", pkg])
    else:
        candidates.append(f"{cleaned}:{cleaned.split('/')[-1]}")

    for candidate in candidates:
        if candidate in GATE_TRIGGERS:
            return candidate

    return cleaned


def is_gate_triggered(
    gate: str,
    changed_files: Sequence[str],
    repo_root: Path | None = None,
) -> bool:
    """Return True if any changed file satisfies the trigger criteria for the gate."""
    normalized_gate = normalize_gate_label(gate)
    patterns = GATE_TRIGGERS.get(normalized_gate)
    if patterns is None:
        return False

    for pattern in patterns:
        if pattern == "LINT.IfChange":
            if any(file_contains_ifchange(f, repo_root=repo_root) for f in changed_files):
                return True
        elif any(matches_pattern(pattern, f) for f in changed_files):
            return True

    return False


def filter_gates(
    gates: Sequence[str],
    changed_files: Sequence[str],
    *,
    run_all: bool = False,
    repo_root: Path | None = None,
) -> list[str]:
    """Filter static analysis gates to those triggered by changed files or core invalidation."""
    if run_all or any(is_core_file(f) for f in changed_files):
        return list(gates)

    return [gate for gate in gates if is_gate_triggered(gate, changed_files, repo_root=repo_root)]


def filter_generators(
    changed_files: Sequence[str],
    *,
    run_all: bool = False,
) -> list[str]:
    """Filter fix generators to those triggered by changed files or core configuration drift."""
    if run_all or any(is_core_file(f) for f in changed_files):
        return list(GENERATOR_TRIGGERS.keys())

    selected: list[str] = []
    for gen_name, patterns in GENERATOR_TRIGGERS.items():
        if any(matches_pattern(pattern, f) for pattern in patterns for f in changed_files):
            selected.append(gen_name)

    return selected
