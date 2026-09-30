#!/usr/bin/env python3
"""Generate root readme.md from src/infra/docs/readme.md with repo-relative paths."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SOURCE = Path("src/infra/docs/readme.md")
TARGET = Path("readme.md")
DOC_PREFIX = "src/infra/docs"


def rewrite_links(content: str, prefix: str = DOC_PREFIX) -> str:
    """Rewrite relative markdown and HTML links to be repository-root relative."""

    def html_sub(m: re.Match[str]) -> str:
        attr, path = m.group(1), m.group(2)
        if path.startswith(("http://", "https://", "mailto:", "#", "/")):
            return m.group(0)
        clean = path.removeprefix("./")
        return f'{attr}="{prefix}/{clean}"'

    content = re.sub(r'\b(src|href)="([^"]+)"', html_sub, content)

    def md_sub(m: re.Match[str]) -> str:
        text, path = m.group(1), m.group(2)
        if path.startswith(("http://", "https://", "mailto:", "#", "/")):
            return m.group(0)
        clean = path.removeprefix("./")
        return f"[{text}]({prefix}/{clean})"

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", md_sub, content)


def generate(root: Path) -> str:
    """Return root readme.md generated from the infra documentation readme."""
    source_path = root / SOURCE
    return rewrite_links(source_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    """Check that root readme.md matches the generator, or rewrite it with --fix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="rewrite root readme.md")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd())),
        help="repository root",
    )
    arguments = parser.parse_args(argv)
    target = arguments.root / TARGET
    expected = generate(arguments.root)
    if arguments.fix:
        if target.is_symlink():
            target.unlink()
        target.write_text(expected, encoding="utf-8")
        return 0
    if not target.is_file() or target.read_text(encoding="utf-8") != expected:
        sys.stderr.write(f"{TARGET} is stale: run `python3 {SOURCE.parent}/readme.py --fix`\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
