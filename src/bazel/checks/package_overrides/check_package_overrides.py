#!/usr/bin/env python3
"""Audit and validate package manager dependency overrides across workspace manifests and lockfiles."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml


def load_manifest_overrides(
    repo_root: Path,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, Any]]:
    """Loads overrides defined in package.json, pnpm-workspace.yaml, and pnpm-lock.yaml.

    Returns:
        tuple containing (package_json_overrides, workspace_overrides, lockfile_overrides, lock_data).
    """
    pkg_path = repo_root / "package.json"
    ws_path = repo_root / "pnpm-workspace.yaml"
    lock_path = repo_root / "pnpm-lock.yaml"

    pkg_overrides: dict[str, str] = {}
    if pkg_path.is_file():
        try:
            pkg_data = json.loads(pkg_path.read_text(encoding="utf-8"))
            raw = pkg_data.get("overrides", {})
            if isinstance(raw, dict):
                pkg_overrides = {str(k): str(v) for k, v in raw.items()}
        except (json.JSONDecodeError, OSError) as err:
            sys.stderr.write(f"Warning: Failed to parse {pkg_path}: {err}\n")

    ws_overrides: dict[str, str] = {}
    if ws_path.is_file():
        try:
            ws_data = yaml.safe_load(ws_path.read_text(encoding="utf-8"))
            if isinstance(ws_data, dict):
                raw = ws_data.get("overrides", {})
                if isinstance(raw, dict):
                    ws_overrides = {str(k): str(v) for k, v in raw.items()}
        except (yaml.YAMLError, OSError) as err:
            sys.stderr.write(f"Warning: Failed to parse {ws_path}: {err}\n")

    lock_overrides: dict[str, str] = {}
    lock_data: dict[str, Any] = {}
    if lock_path.is_file():
        try:
            loaded = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                lock_data = loaded
                raw = lock_data.get("overrides", {})
                if isinstance(raw, dict):
                    lock_overrides = {str(k): str(v) for k, v in raw.items()}
        except (yaml.YAMLError, OSError) as err:
            sys.stderr.write(f"Warning: Failed to parse {lock_path}: {err}\n")

    return pkg_overrides, ws_overrides, lock_overrides, lock_data


def find_active_dependents(lock_data: dict[str, Any], pkg_name: str) -> list[str]:
    """Finds all distinct package snapshots in pnpm-lock.yaml that depend on pkg_name."""
    dependents: set[str] = set()

    snapshots = lock_data.get("snapshots", {})
    if isinstance(snapshots, dict):
        for snap_id, snap_info in snapshots.items():
            if not isinstance(snap_info, dict):
                continue
            deps = {
                **snap_info.get("dependencies", {}),
                **snap_info.get("optionalDependencies", {}),
            }
            if pkg_name in deps:
                clean_name = str(snap_id).split("(", 1)[0]
                dependents.add(clean_name)

    importers = lock_data.get("importers", {})
    if isinstance(importers, dict):
        for imp_name, imp_info in importers.items():
            if not isinstance(imp_info, dict):
                continue
            deps = {
                **imp_info.get("dependencies", {}),
                **imp_info.get("devDependencies", {}),
            }
            if pkg_name in deps:
                label = "root" if imp_name == "." else imp_name
                dependents.add(f"workspace importer '{label}'")

    return sorted(dependents)


def validate_package_overrides(
    repo_root: Path,
) -> tuple[list[str], list[str]]:
    """Validates that overrides across workspace files are synchronized and actively needed.

    Returns:
        tuple containing (errors, info_messages).
    """
    errors: list[str] = []
    info: list[str] = []

    pkg_overrides, ws_overrides, lock_overrides, lock_data = load_manifest_overrides(repo_root)

    # 1. Verify synchronization between package.json and pnpm-workspace.yaml
    if pkg_overrides != ws_overrides:
        missing_in_ws = sorted(set(pkg_overrides) - set(ws_overrides))
        missing_in_pkg = sorted(set(ws_overrides) - set(pkg_overrides))
        differing = sorted(
            k for k in set(pkg_overrides) & set(ws_overrides) if pkg_overrides[k] != ws_overrides[k]
        )
        diff_parts = []
        if missing_in_ws:
            diff_parts.append(f"missing in pnpm-workspace.yaml: {missing_in_ws}")
        if missing_in_pkg:
            diff_parts.append(f"missing in package.json: {missing_in_pkg}")
        if differing:
            diff_parts.append(
                f"differing versions: {[(k, pkg_overrides[k], ws_overrides[k]) for k in differing]}"
            )
        errors.append(
            f"Override mismatch between package.json and pnpm-workspace.yaml: {'; '.join(diff_parts)}"
        )

    # 2. Verify synchronization with pnpm-lock.yaml if present
    if lock_data and pkg_overrides != lock_overrides:
        errors.append(
            "pnpm-lock.yaml overrides do not match package.json overrides. "
            "Run 'pnpm install --lockfile-only' to regenerate lockfile."
        )

    # 3. Verify that each declared override is actually consumed in the dependency graph
    if lock_data:
        for pkg_name, pinned_ver in sorted(pkg_overrides.items()):
            dependents = find_active_dependents(lock_data, pkg_name)
            if not dependents:
                errors.append(
                    f"Override '{pkg_name}@{pinned_ver}' is unused: no package in the dependency "
                    f"graph depends on '{pkg_name}'. This override is obsolete and should be removed."
                )
            else:
                formatted = ", ".join(dependents[:3])
                if len(dependents) > 3:
                    formatted += f" (+{len(dependents) - 3} more)"
                info.append(
                    f"Override '{pkg_name}@{pinned_ver}' is active (required by {formatted})"
                )

    return errors, info


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for checking package overrides."""
    parser = argparse.ArgumentParser(
        description="Verify package manager dependency overrides are synchronized and actively required"
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        help="Root workspace directory to inspect (defaults to BUILD_WORKSPACE_DIRECTORY or cwd)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print active override dependents summary even on success",
    )
    args = parser.parse_args(argv)

    workspace_root_env = os.getenv("BUILD_WORKSPACE_DIRECTORY")
    workspace_root = args.workspace_dir or (
        Path(workspace_root_env) if workspace_root_env else Path.cwd()
    )

    errors, info = validate_package_overrides(workspace_root)

    if errors:
        for err in errors:
            sys.stderr.write(f"ERROR: {err}\n")
        return 1

    if args.verbose or not os.getenv("BUILD_WORKSPACE_DIRECTORY"):
        for msg in info:
            sys.stdout.write(f"{msg}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
