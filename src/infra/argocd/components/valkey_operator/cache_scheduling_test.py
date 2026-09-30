"""Verify operator-generated compiler cache Pods retain team scheduling intent."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml


class CacheSchedulingTest(unittest.TestCase):
    def test_generated_cache_pods_match_declared_priority(self) -> None:
        operator = list(yaml.safe_load_all(Path(sys.argv[1]).read_text(encoding="utf-8")))
        cache = list(yaml.safe_load_all(Path(sys.argv[2]).read_text(encoding="utf-8")))
        policy = next(item for item in operator if item and item["kind"] == "ClusterPolicy")
        rule = next(
            item
            for item in policy["spec"]["rules"]
            if item["name"] == "label-compile-cache-scheduling"
        )
        cluster = next(item for item in cache if item and item["kind"] == "ValkeyCluster")
        matching = rule["match"]["any"]
        assert len(matching) == 1
        resource = matching[0]["resources"]
        assert resource["kinds"] == ["Pod"]
        assert (
            policy["metadata"]["annotations"]["pod-policies.kyverno.io/autogen-controllers"]
            == "none"
        )
        assert set(resource["operations"]) == {"CREATE", "UPDATE"}
        assert resource["selector"]["matchLabels"] == {
            "app.kubernetes.io/managed-by": "valkey-operator",
            "app.kubernetes.io/name": "valkey",
            "valkey.io/cluster": cluster["metadata"]["name"],
        }
        patch = rule["mutate"]["patchStrategicMerge"]
        assert "spec" not in patch
        labels = patch["metadata"]["labels"]
        assert labels["availability-class"] == labels["kueue.x-k8s.io/queue-name"] == "be"
        assert labels["latency-class"] == "ls"
        assert cluster["spec"]["scheduling"]["priorityClassName"] == (
            f"{labels['availability-class']}-{labels['latency-class']}"
        )
        assert cluster["spec"]["workloadType"] == "Deployment"
        assert "persistence" not in cluster["spec"]

    def test_cache_autoscaling_preserves_memory_limit_above_maxmemory(self) -> None:
        cache = list(yaml.safe_load_all(Path(sys.argv[2]).read_text(encoding="utf-8")))
        cluster = next(item for item in cache if item and item["kind"] == "ValkeyCluster")
        autoscaler = next(
            item for item in cache if item and item["kind"] == "VerticalPodAutoscaler"
        )
        assert autoscaler["spec"]["resourcePolicy"]["containerPolicies"] == [
            {"containerName": "*", "controlledValues": "RequestsOnly"}
        ]
        assert autoscaler["spec"]["updatePolicy"]["updateMode"] == "InPlace"
        assert int(autoscaler["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < int(
            cluster["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
        )
        assert cluster["spec"]["config"]["maxmemory"] == "384mb"
        assert cluster["spec"]["resources"]["limits"]["memory"] == "512Mi"


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
