#!/usr/bin/env python3
"""Validate Argo CD component coverage, profile and provider references, and referenced Helm value files."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def _check_components(root: Path, fleet_contents: dict[Path, str]) -> list[str]:
    """Validate that every component directory is referenced in fleet definitions."""
    components_root = root / "src/infra/argocd/components"
    comp_dirs = (
        [d for d in components_root.iterdir() if d.is_dir()] if components_root.is_dir() else []
    )
    obs = root / "src/infra/definitions/observability"
    if obs.is_dir():
        comp_dirs.append(obs)

    errors: list[str] = []
    for cd in comp_dirs:
        if not any(cd.rglob("*")):
            continue
        rel_cd = str(cd.relative_to(root))
        pattern = re.compile(rf"{re.escape(rel_cd)}(/|$)", re.MULTILINE)
        if not any(pattern.search(txt) for txt in fleet_contents.values()):
            errors.append(
                f"Unreferenced component: {rel_cd} (expected fleet child Application or valueFiles)"
            )
    return errors


def _find_component_value_files(components_root: Path) -> list[Path]:
    if not components_root.is_dir():
        return []
    value_files: list[Path] = []
    for comp in components_root.iterdir():
        if not comp.is_dir():
            continue
        for subdir in ("providers", "profiles"):
            d = comp / "helm" / subdir
            if d.is_dir():
                value_files.extend(p for p in d.rglob("*.yaml") if p.is_file())
    return value_files


def _is_value_file_referenced(
    vf: Path,
    root: Path,
    fleet_contents: Mapping[Path, str],
) -> bool:
    vf_rel = str(vf.relative_to(root))
    comp_dir = vf_rel.split("/helm/", maxsplit=1)[0]
    rel_val = vf_rel.split(comp_dir + "/helm/")[1]
    val_kind = rel_val.split("/")[0]
    val_dir = str(vf.parent.relative_to(root)) + "/"
    rel_val_dir = str(Path(rel_val).parent) + "/"

    candidate_caps = [p for p, txt in fleet_contents.items() if (comp_dir + "/") in txt]
    for cap in candidate_caps:
        txt = fleet_contents[cap]
        if vf_rel in txt or rel_val in txt:
            return True
        if val_kind in {"providers", "profiles"} and (val_dir in txt or rel_val_dir in txt):
            return True
    return False


def _check_value_files(root: Path, fleet_contents: dict[Path, str]) -> list[str]:
    """Validate that provider and profile value files are referenced by sourcing applications."""
    value_files = _find_component_value_files(root / "src/infra/argocd/components")
    errors: list[str] = []
    for vf in value_files:
        if not _is_value_file_referenced(vf, root, fleet_contents):
            vf_rel = str(vf.relative_to(root))
            comp_dir = vf_rel.split("/helm/", maxsplit=1)[0]
            errors.append(
                f"Unreferenced value file: {vf_rel} (expected valueFiles entry sourcing {comp_dir})"
            )
    return errors


def _check_template_references(root: Path, all_fleet_text: str) -> list[str]:
    """Validate that $values/ paths referenced in fleet templates exist on disk."""
    dir_ref_pattern = re.compile(
        r"\$values/(src/infra/argocd/components/[A-Za-z0-9_/-]+/(?:providers|profiles)/)"
    )
    file_ref_pattern = re.compile(
        r"\$values/(src/infra/argocd/components/[A-Za-z0-9_/-]+/(?:providers|profiles)/[A-Za-z0-9_.-]+[.]yaml)"
    )

    errors: list[str] = []
    for d_ref in sorted(set(dir_ref_pattern.findall(all_fleet_text))):
        target_dir = root / d_ref
        first_file = next(target_dir.glob("*.yaml"), None) if target_dir.is_dir() else None
        if not first_file:
            errors.append(
                f"Broken $values directory reference: $values/{d_ref} (no YAML files found)"
            )

    for f_ref in sorted(set(file_ref_pattern.findall(all_fleet_text))):
        target_file = root / f_ref
        if not target_file.is_file():
            errors.append(f"Broken $values file reference: $values/{f_ref} (file does not exist)")
    return errors


def validate_argocd_links(root: Path) -> list[str]:
    """Validate all fleet components, value files, and template references."""
    fleet_roots = [
        root / "src/infra/argocd/apps",
        root / "src/infra/argocd/components/team_lane/helm/templates",
    ]
    fleet_files = [p for d in fleet_roots if d.is_dir() for p in d.rglob("*") if p.is_file()]
    fleet_contents = {p: p.read_text(encoding="utf-8", errors="ignore") for p in fleet_files}
    all_fleet_text = "\n".join(fleet_contents.values())

    errors: list[str] = []
    errors.extend(_check_components(root, fleet_contents))
    errors.extend(_check_value_files(root, fleet_contents))
    errors.extend(_check_template_references(root, all_fleet_text))
    return errors


def main() -> int:
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd()))
    errors = validate_argocd_links(root)
    if errors:
        for _err in errors:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
