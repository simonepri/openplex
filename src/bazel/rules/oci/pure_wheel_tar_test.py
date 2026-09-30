"""Defend isolated wheel imports and reject host-dependent or unsafe runtime archives."""

from __future__ import annotations

import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.bazel.rules.oci.pure_wheel_tar import package_wheels


class PureWheelTarTest(unittest.TestCase):
    def test_preserves_modules_and_metadata_in_isolated_import_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "example-1-py3-none-any.whl"
            files = {
                "example/__init__.py": b"VALUE = 4\n",
                "example-1.dist-info/METADATA": b"Version: 1\n",
            }
            with zipfile.ZipFile(wheel, "w") as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
            destination = root / "layer.tar"
            package_wheels(destination, [wheel])
            with tarfile.open(destination) as archive:
                for name, expected in files.items():
                    member = archive.getmember(f"opt/python/{name}")
                    stream = archive.extractfile(member)
                    assert stream is not None
                    assert stream.read() == expected
                    assert member.mtime == 0

    def test_rejects_platform_wheels_and_escaping_paths(self) -> None:
        cases = [
            ("example-1-cp313-cp313-macosx_11_0_arm64.whl", "example.py"),
            ("example-1-py3-none-any.whl", "../outside.py"),
            ("example-1-py3-none-any.whl", "/outside.py"),
        ]
        for filename, member in cases:
            with (
                self.subTest(filename=filename, member=member),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                wheel = root / filename
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr(member, b"invalid")
                with self.assertRaises(ValueError):
                    package_wheels(root / "layer.tar", [wheel])


if __name__ == "__main__":
    unittest.main()
