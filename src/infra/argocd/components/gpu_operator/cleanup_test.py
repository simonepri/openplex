"""Verify NFD cleanup runs while its namespace and node permissions still exist."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml


class CleanupTest(unittest.TestCase):
    def test_node_label_cleanup_precedes_namespace_deletion(self) -> None:
        resources = [
            item
            for path in sys.argv[1:]
            for item in yaml.safe_load_all(Path(path).read_text(encoding="utf-8"))
            if item
        ]
        cleanup = [
            item
            for item in resources
            if item["metadata"]["name"] == "gpu-operator-node-feature-discovery-prune"
        ]
        assert {item["kind"] for item in cleanup} == {
            "ServiceAccount",
            "ClusterRole",
            "ClusterRoleBinding",
            "Job",
        }
        assert len(cleanup) == 4
        for item in resources:
            assert "argocd.argoproj.io/hook" not in item["metadata"].get("annotations", {})
            assert "post-delete" not in item["metadata"].get("annotations", {}).get(
                "helm.sh/hook", ""
            )
        for item in cleanup:
            annotations = item["metadata"]["annotations"]
            assert annotations["helm.sh/hook"] == "pre-delete"
            assert (
                annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
            )
        job = next(item for item in cleanup if item["kind"] == "Job")
        pod = job["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "gpu-operator-node-feature-discovery-prune"
        assert pod["containers"][0]["command"] == ["nfd-master"]
        assert pod["containers"][0]["args"] == ["-prune"]
        master = next(
            item
            for item in resources
            if item["kind"] == "Deployment"
            and item["metadata"]["name"].endswith("node-feature-discovery-master")
        )
        assert (
            pod["containers"][0]["image"]
            == master["spec"]["template"]["spec"]["containers"][0]["image"]
        )
        assert job["metadata"]["namespace"] == "gpu-system"
        role = next(item for item in cleanup if item["kind"] == "ClusterRole")
        assert role["rules"] == [
            {
                "apiGroups": [""],
                "resources": ["nodes", "nodes/status"],
                "verbs": ["get", "list", "patch", "update"],
            }
        ]


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
