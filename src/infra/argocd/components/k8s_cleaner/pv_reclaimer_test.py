"""Verify Cleaner pv-reclaimer conforms to upstream CRD schema and evaluate/transform contracts."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, ClassVar, override

import jsonschema
import yaml


class PvReclaimerTest(unittest.TestCase):
    helm: ClassVar[str]
    cleaner_crd_schema: ClassVar[dict[str, Any]]
    cleaner_manifest: ClassVar[dict[str, Any]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        tools = [Path(value).resolve() for value in " ".join(sys.argv[1:-2]).split()]
        PvReclaimerTest.helm = str(next(path for path in tools if path.name == "helm"))
        chart_pkg = Path(sys.argv[-2]).resolve()
        cleaner_file = Path(sys.argv[-1]).resolve()

        PvReclaimerTest.cleaner_manifest = yaml.safe_load(cleaner_file.read_text(encoding="utf-8"))

        result = subprocess.run(
            [
                PvReclaimerTest.helm,
                "template",
                "k8s-cleaner",
                str(chart_pkg),
                "--include-crds",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        cleaner_crd = next(
            doc
            for doc in documents
            if doc.get("kind") == "CustomResourceDefinition"
            and doc.get("metadata", {}).get("name") == "cleaners.apps.projectsveltos.io"
        )
        PvReclaimerTest.cleaner_crd_schema = cleaner_crd["spec"]["versions"][0]["schema"][
            "openAPIV3Schema"
        ]

    def test_cleaner_manifest_conforms_to_crd_schema(self) -> None:
        spec_schema = self.cleaner_crd_schema["properties"]["spec"]
        assert "dryRun" not in spec_schema.get("properties", {}), "CRD schema lacks dryRun"

        spec = self.cleaner_manifest["spec"]
        jsonschema.validate(instance=spec, schema=spec_schema)
        for key in spec:
            assert key in spec_schema["properties"], f"Undeclared key {key} in Cleaner spec"

    def test_cleaner_action_is_transform(self) -> None:
        assert self.cleaner_manifest["spec"]["action"] == "Transform"

    def test_transform_contract_returns_mutated_resource_table(self) -> None:
        transform = self.cleaner_manifest["spec"]["transform"]
        assert 'obj.spec.persistentVolumeReclaimPolicy = "Delete"' in transform
        assert "hs.resource = obj" in transform
        assert "return hs" in transform

    def test_selector_evaluation_protects_active_and_opted_out_volumes(self) -> None:
        selectors = self.cleaner_manifest["spec"]["resourcePolicySet"]["resourceSelectors"]
        assert len(selectors) == 1
        pv_selector = selectors[0]
        assert pv_selector["kind"] == "PersistentVolume"
        assert pv_selector["group"] == ""
        assert pv_selector["version"] == "v1"

        lua_eval = pv_selector["evaluate"]
        assert 'obj.status.phase == "Released"' in lua_eval
        assert 'persistentVolumeReclaimPolicy == "Delete"' in lua_eval
        assert "reclaim.storage.k8s.io/do-not-reclaim" in lua_eval
        assert "do-not-reclaim" in lua_eval
        assert "ttl" in lua_eval or "ttlSeconds" in lua_eval


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
