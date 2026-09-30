"""Verify node-local scrape jobs select only ready target pods and preserve cluster identity."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any

import yaml


class ScrapeTest(unittest.TestCase):
    config: dict[str, Any]
    pipeline: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        resources = list(yaml.safe_load_all(Path(sys.argv[1]).read_text(encoding="utf-8")))
        cls.config = next(
            yaml.safe_load(item["data"]["relay"])
            for item in resources
            if item and item["kind"] == "ConfigMap" and "relay" in item.get("data", {})
        )
        cls.pipeline = cls.config["service"]["pipelines"]["metrics"]

    def jobs(self, name: str) -> list[dict[str, Any]]:
        return [
            job
            for receiver in self.pipeline["receivers"]
            if receiver.startswith("prometheus")
            for job in self.config["receivers"][receiver]["config"]["scrape_configs"]
            if job["job_name"] == name
        ]

    def test_metrics_pipeline_labels_every_series_with_the_cluster_name(self) -> None:
        assert "resource/cluster_name" in self.pipeline["processors"]
        assert any(
            item["key"] == "k8s.cluster.name" and item["value"] == "${env:K8S_CLUSTER_NAME}"
            for item in self.config["processors"]["resource/cluster_name"]["attributes"]
        )

    def test_coredns_scrape_targets_ready_kube_dns_metrics_port_on_the_node(self) -> None:
        jobs = self.jobs("coredns")
        assert len(jobs) == 1
        job = jobs[0]
        assert job["honor_labels"] is True
        assert job["kubernetes_sd_configs"] == [
            {
                "role": "pod",
                "namespaces": {"names": ["kube-system"]},
                "selectors": [{"role": "pod", "field": "spec.nodeName=${env:K8S_NODE_NAME}"}],
            }
        ]
        for app, port, ready, accepted in (
            ("kube-dns", "9153", "true", True),
            ("kube-dns", "53", "true", False),
            ("kube-dns", "9153", "false", False),
            ("other-dns", "9153", "true", False),
        ):
            with self.subTest(app=app, port=port, ready=ready):
                labels = {
                    "__meta_kubernetes_pod_label_k8s_app": app,
                    "__meta_kubernetes_pod_container_port_number": port,
                    "__meta_kubernetes_pod_ready": ready,
                }
                keeps = job["relabel_configs"]
                assert all(rule["action"] == "keep" for rule in keeps)
                matches = all(
                    re.fullmatch(
                        rule["regex"], ";".join(labels[label] for label in rule["source_labels"])
                    )
                    for rule in keeps
                )
                assert matches is accepted

    def test_pricing_scrape_excludes_other_ports_and_preserves_cluster_identity(self) -> None:
        jobs = self.jobs("opencost")
        assert len(jobs) == 1
        job = jobs[0]
        assert job["honor_labels"] is True
        assert job["metric_relabel_configs"] == [
            {"target_label": "cluster", "replacement": "${env:K8S_CLUSTER_NAME}"}
        ]
        assert job["kubernetes_sd_configs"] == [
            {
                "role": "pod",
                "namespaces": {"names": ["opencost"]},
                "selectors": [{"role": "pod", "field": "spec.nodeName=${env:K8S_NODE_NAME}"}],
            }
        ]
        for app, container, port, ready, accepted in (
            ("opencost", "opencost", "9003", "true", True),
            ("opencost", "opencost", "8081", "true", False),
            ("opencost", "opencost-ui", "9090", "true", False),
            ("opencost", "opencost", "9003", "false", False),
            ("other", "opencost", "9003", "true", False),
        ):
            with self.subTest(app=app, container=container, port=port, ready=ready):
                labels = {
                    "__meta_kubernetes_pod_label_app_kubernetes_io_name": app,
                    "__meta_kubernetes_pod_container_name": container,
                    "__meta_kubernetes_pod_container_port_number": port,
                    "__meta_kubernetes_pod_ready": ready,
                }
                keeps = job["relabel_configs"]
                assert all(rule["action"] == "keep" for rule in keeps)
                matches = all(
                    re.fullmatch(
                        rule["regex"], ";".join(labels[label] for label in rule["source_labels"])
                    )
                    for rule in keeps
                )
                assert matches is accepted


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
