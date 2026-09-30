"""Preserve or insert purpose docstring headers in BUILD.bazel files during Gazelle generation."""

import re
from pathlib import Path


def normalize(content: str, package: str) -> str:
    """Keep an existing purpose first, or describe a newly generated package."""
    purpose = re.search(r'(?m)^"""[\s\S]*?"""\n', content)
    if purpose is not None:
        if purpose.start() == 0:
            return content
        body = content[: purpose.start()] + content[purpose.end() :]
        return purpose.group().rstrip() + "\n\n" + body.lstrip("\n")
    return f'"""Build targets for {package}."""\n\n' + content.lstrip("\n")


def main() -> None:
    for path in sorted(Path("src").rglob("BUILD.bazel")):
        content = path.read_text()
        updated = normalize(content, path.parent.as_posix())
        if updated != content:
            path.write_text(updated)


if __name__ == "__main__":
    main()
