"""Verify third_party_image macro targets, deterministic timestamps, and structure."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any


def read_blob(layout: Path, digest: str) -> dict[str, Any]:
    algorithm, hex_digest = digest.split(":")
    blob_path = layout / "blobs" / algorithm / hex_digest
    return json.loads(blob_path.read_text(encoding="utf-8"))


def get_manifest_descriptors(layout: Path) -> list[dict[str, Any]]:
    index_json = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    manifests = index_json.get("manifests", [])
    if (
        len(manifests) == 1
        and manifests[0].get("mediaType") == "application/vnd.oci.image.index.v1+json"
    ):
        index_doc = read_blob(layout, manifests[0]["digest"])
        return index_doc.get("manifests", [])
    return manifests


class ThirdPartyImageMacroTest(unittest.TestCase):
    def test_multiarch_image_structure_and_determinism(self) -> None:
        multiarch_layout = Path(sys.argv[1])
        manifest_descriptors = get_manifest_descriptors(multiarch_layout)
        self.assertEqual(len(manifest_descriptors), 2)

        architectures_found = set()
        for desc in manifest_descriptors:
            manifest = read_blob(multiarch_layout, desc["digest"])
            config = read_blob(multiarch_layout, manifest["config"]["digest"])

            arch = config.get("architecture")
            architectures_found.add(arch)
            self.assertEqual(config.get("os"), "linux")

            # Deterministic timestamps verification
            self.assertEqual(config.get("created"), "1970-01-01T00:00:00Z")

            container_config = config.get("config", {})
            env_vars = dict(item.split("=", 1) for item in container_config.get("Env", []))
            self.assertEqual(env_vars.get("SOURCE_DATE_EPOCH"), "0")
            self.assertEqual(env_vars.get("TEST_ENV"), "multiarch_ok")
            self.assertEqual(container_config.get("Entrypoint"), ["/bin/sh"])

        self.assertEqual(architectures_found, {"amd64", "arm64"})

    def test_singlearch_image_structure_and_determinism(self) -> None:
        singlearch_layout = Path(sys.argv[2])
        manifest_descriptors = get_manifest_descriptors(singlearch_layout)
        self.assertEqual(len(manifest_descriptors), 1)

        manifest = read_blob(singlearch_layout, manifest_descriptors[0]["digest"])
        config = read_blob(singlearch_layout, manifest["config"]["digest"])

        self.assertEqual(config.get("architecture"), "amd64")
        self.assertEqual(config.get("os"), "linux")
        self.assertEqual(config.get("created"), "1970-01-01T00:00:00Z")

        container_config = config.get("config", {})
        env_vars = dict(item.split("=", 1) for item in container_config.get("Env", []))
        self.assertEqual(env_vars.get("SOURCE_DATE_EPOCH"), "0")
        self.assertEqual(env_vars.get("TEST_ENV"), "singlearch_ok")
        self.assertEqual(container_config.get("Cmd"), ["hello"])

    def test_publish_and_load_executables_exist(self) -> None:
        multiarch_publish = Path(sys.argv[3])
        singlearch_publish = Path(sys.argv[4])
        self.assertTrue(multiarch_publish.exists())
        self.assertTrue(singlearch_publish.exists())
        self.assertTrue(os.access(multiarch_publish, os.X_OK))
        self.assertTrue(os.access(singlearch_publish, os.X_OK))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *sys.argv[5:]])
