"""Check package layers retain files and links without replacing merged-/usr aliases."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from src.bazel.rules.oci.usrmerged_tar import merged_path, normalize_archive


class UsrmergedTarTest(unittest.TestCase):
    def test_package_directories_cannot_replace_base_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "packages.tar"
            destination = Path(directory) / "layer.tar"
            with tarfile.open(source, "w") as archive:
                for root in ("bin", "lib", "lib64", "sbin"):
                    member = tarfile.TarInfo(f"./{root}")
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                    member = tarfile.TarInfo(f"./{root}/tool")
                    member.size = 4
                    member.mode = 0o755
                    archive.addfile(member, io.BytesIO(b"tool"))
                link = tarfile.TarInfo("usr/bin/compiler")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../bin/tool"
                archive.addfile(link)
            normalize_archive(source, destination)
            with tarfile.open(destination) as archive:
                for root in ("bin", "lib", "lib64", "sbin"):
                    assert root not in archive.getnames()
                    assert archive.getmember(f"usr/{root}").isdir()
                    assert archive.getmember(f"usr/{root}/tool").mode == 0o755
                    content = archive.extractfile(f"usr/{root}/tool")
                    assert content is not None
                    assert content.read() == b"tool"
                assert archive.getmember("usr/bin/compiler").linkname == "tool"

    def test_rejects_paths_outside_the_archive_root(self) -> None:
        for path in ("/bin/tool", "../bin/tool", "usr/../../bin/tool"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                merged_path(path)


if __name__ == "__main__":
    unittest.main()
