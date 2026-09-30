"""Package locked platform-independent Python wheels into an isolated runtime import path."""

from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def package_wheels(destination: Path, wheels: list[Path]) -> None:
    """Preserve wheel modules and distribution metadata without copying host binaries."""
    files: dict[str, bytes] = {}
    for wheel in wheels:
        if not wheel.name.endswith("-none-any.whl"):
            raise ValueError(f"runtime wheel must be platform-independent: {wheel.name}")
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"wheel path must be relative and contained: {path}")
                if member.is_dir():
                    continue
                if any(part.endswith(".data") for part in path.parts):
                    raise ValueError(
                        f"runtime wheel requires unsupported installation scheme: {path}"
                    )
                content = archive.read(member)
                name = f"opt/python/{path}"
                if name in files and files[name] != content:
                    raise ValueError(f"runtime wheels contain conflicting path: {path}")
                files[name] = content
    with tarfile.open(destination, "w") as output:
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o444
            output.addfile(member, io.BytesIO(content))


if __name__ == "__main__":
    package_wheels(Path(sys.argv[1]), [Path(value) for value in sys.argv[2:]])
