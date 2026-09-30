"""Move package archive paths under /usr while preserving merged-/usr base image aliases."""

from __future__ import annotations

import copy
import posixpath
import sys
import tarfile
from pathlib import Path


def normalize_archive(source: Path, destination: Path) -> None:
    with tarfile.open(source) as archive, tarfile.open(destination, "w") as output:
        members = {}
        for original in archive:
            member = copy.copy(original)
            member.name = merged_path(original.name)
            if member.islnk():
                member.linkname = merged_path(original.linkname)
            elif member.issym() and not original.linkname.startswith("/"):
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(original.name), original.linkname)
                )
                member.linkname = posixpath.relpath(
                    merged_path(target), posixpath.dirname(member.name)
                )
            members[member.name] = (original, member)
        for name in sorted(members):
            original, member = members[name]
            content = archive.extractfile(original) if original.isfile() else None
            output.addfile(member, content)


def merged_path(path: str) -> str:
    parts = Path(path).parts
    if path.startswith("/") or ".." in parts:
        raise ValueError(f"archive path must be relative and contained: {path}")
    normalized = posixpath.normpath(path)
    if normalized.split("/", 1)[0] in {"bin", "lib", "lib64", "sbin"}:
        return f"usr/{normalized}"
    return normalized


if __name__ == "__main__":
    normalize_archive(Path(sys.argv[1]), Path(sys.argv[2]))
