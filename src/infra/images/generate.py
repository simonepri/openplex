"""Generates the infrastructure container image digest map in ApplicationSet manifests."""

import argparse
import difflib
import json
import os
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

START_MARKER = "# BEGIN GENERATED INFRASTRUCTURE IMAGES"
END_MARKER = "# END GENERATED INFRASTRUCTURE IMAGES"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate infrastructure container image map in ApplicationSet manifests."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify files are up to date without writing changes.",
    )
    parser.add_argument(
        "--inventory",
        type=pathlib.Path,
        default=None,
        help="Path to infrastructure-images.json.",
    )
    parser.add_argument(
        "--ctrl",
        type=pathlib.Path,
        default=None,
        help="Path to ctrl.yaml.",
    )
    parser.add_argument(
        "--cells",
        type=pathlib.Path,
        default=None,
        help="Path to cells.yaml.",
    )
    args = parser.parse_args(argv)

    inventory_path, ctrl_path, cells_path = resolve_paths(
        inventory=args.inventory,
        ctrl=args.ctrl,
        cells=args.cells,
    )

    success = process_manifests(
        inventory_path=inventory_path,
        ctrl_path=ctrl_path,
        cells_path=cells_path,
        check=args.check,
    )
    return 0 if success else 1


def resolve_paths(
    inventory: pathlib.Path | None,
    ctrl: pathlib.Path | None,
    cells: pathlib.Path | None,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    workspace_root = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    base_dir = pathlib.Path(workspace_root) if workspace_root else pathlib.Path.cwd()

    resolved_inventory = inventory or (base_dir / "src/infra/images/infrastructure-images.json")
    resolved_ctrl = ctrl or (base_dir / "src/infra/argocd/apps/ctrl.yaml")
    resolved_cells = cells or (base_dir / "src/infra/argocd/apps/cells.yaml")

    return (
        resolved_inventory.resolve(),
        resolved_ctrl.resolve(),
        resolved_cells.resolve(),
    )


def process_manifests(
    inventory_path: pathlib.Path,
    ctrl_path: pathlib.Path,
    cells_path: pathlib.Path,
    check: bool,
) -> bool:
    inventory_data = read_json(inventory_path)
    generated_block = render_image_block(inventory_data)

    ctrl_original = ctrl_path.read_text(encoding="utf-8")
    cells_original = cells_path.read_text(encoding="utf-8")

    ctrl_updated = replace_generated_block(ctrl_original, generated_block)
    cells_updated = replace_generated_block(cells_original, generated_block)

    is_ctrl_diff = ctrl_original != ctrl_updated
    is_cells_diff = cells_original != cells_updated

    if check:
        if is_ctrl_diff or is_cells_diff:
            print_diff(str(ctrl_path), ctrl_original, ctrl_updated)
            print_diff(str(cells_path), cells_original, cells_updated)
            sys.stderr.write("Run `bazel run //src/infra/images:generate` to regenerate.\n")
            return False
        return True

    if is_ctrl_diff:
        ctrl_path.write_text(ctrl_updated, encoding="utf-8")
    if is_cells_diff:
        cells_path.write_text(cells_updated, encoding="utf-8")
    return True


def read_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def render_image_block(inventory: dict[str, Any]) -> str:
    images: list[dict[str, Any]] = inventory.get("images", [])
    sorted_images = sorted(images, key=lambda x: str(x.get("repository", "")))

    lines = [
        f"    {START_MARKER}",
        "    {{- $infrastructureImages := dict }}",
    ]
    for img in sorted_images:
        repo = img["repository"]
        digest = img["imageDigest"]
        tag = img["tag"]
        line = (
            f'    {{{{- $infrastructureImages = set $infrastructureImages "{repo}" "{digest}" }}}}'
            f"{{{{- /* tag: {tag} */}}}}"
        )
        lines.append(line)
    lines.append(f"    {END_MARKER}")
    return "\n".join(lines)


def replace_generated_block(content: str, replacement_block: str) -> str:
    start_idx = content.find(START_MARKER)
    end_idx = content.find(END_MARKER)
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise ValueError(f"Missing generated block markers '{START_MARKER}' and '{END_MARKER}'")

    line_start = content.rfind("\n", 0, start_idx)
    line_start = 0 if line_start == -1 else line_start + 1

    line_end = content.find("\n", end_idx)
    line_end = len(content) if line_end == -1 else line_end + 1

    return content[:line_start] + replacement_block + "\n" + content[line_end:]


def print_diff(filename: str, original: str, updated: str) -> None:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=filename,
        tofile=f"{filename} (regenerated)",
    )
    diff_text = "".join(diff)
    if diff_text:
        sys.stderr.write(diff_text)


if __name__ == "__main__":
    sys.exit(main())
