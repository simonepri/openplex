"""Tests validating node-problem-detector manifests, monitors, synthetic log replay, and dry-run repair."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

import yaml


class NodeProblemDetectorTest(unittest.TestCase):
    resources: list[dict[str, Any]]
    config_data: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        kustomize_dir = Path(__file__).parent / "kustomize"
        manifest_files = [
            kustomize_dir / "daemonset.yaml",
            kustomize_dir / "configmap.yaml",
            kustomize_dir / "namespace.yaml",
            kustomize_dir / "policy.yaml",
            kustomize_dir / "rbac.yaml",
            kustomize_dir / "repair-cleaner.yaml",
        ]
        cls.resources = []
        for file_path in manifest_files:
            if file_path.exists():
                cls.resources.extend([
                    doc for doc in yaml.safe_load_all(file_path.read_text(encoding="utf-8")) if doc
                ])

        cm = next(
            r
            for r in cls.resources
            if r["kind"] == "ConfigMap" and r["metadata"]["name"] == "node-problem-detector-config"
        )
        cls.config_data = cm["data"]

    def test_daemonset_specifications(self) -> None:
        ds = next(
            r
            for r in self.resources
            if r["kind"] == "DaemonSet" and r["metadata"]["name"] == "node-problem-detector"
        )
        spec = ds["spec"]["template"]["spec"]

        # Explicit automountServiceAccountToken
        self.assertIs(spec.get("automountServiceAccountToken"), True)

        # priorityClassName node-agents
        self.assertEqual(spec.get("priorityClassName"), "node-agents")

        # Tolerations for all taints
        tolerations = spec.get("tolerations", [])
        self.assertTrue(
            any(t.get("operator") == "Exists" and "key" not in t for t in tolerations),
            "Expected tolerations for all taints (operator: Exists)",
        )

        # Pinned image digest
        container = ds["spec"]["template"]["spec"]["containers"][0]
        image = container["image"]
        self.assertIn("@sha256:", image, "Image must be pinned with a SHA256 digest")
        self.assertTrue(
            image.startswith("registry.k8s.io/node-problem-detector/node-problem-detector:")
        )

        # Metrics annotations
        annotations = ds["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations.get("signoz.io/scrape"), "true")
        self.assertEqual(annotations.get("signoz.io/port"), "20257")
        self.assertEqual(annotations.get("signoz.io/path"), "/metrics")

    def test_rbac_and_service_account(self) -> None:
        sa = next(
            r
            for r in self.resources
            if r["kind"] == "ServiceAccount" and r["metadata"]["name"] == "node-problem-detector"
        )
        self.assertIs(sa.get("automountServiceAccountToken"), True)

        cr = next(
            r
            for r in self.resources
            if r["kind"] == "ClusterRole" and r["metadata"]["name"] == "node-problem-detector"
        )
        rules = cr["rules"]
        node_status_rule = next(
            rule for rule in rules if "nodes/status" in rule.get("resources", [])
        )
        self.assertIn("patch", node_status_rule["verbs"])
        self.assertIn("update", node_status_rule["verbs"])

    def test_network_policy_and_vpa(self) -> None:
        netpol = next(
            r
            for r in self.resources
            if r["kind"] == "NetworkPolicy" and r["metadata"]["name"] == "node-problem-detector"
        )
        # Verify ingress for metrics collection
        ingress = netpol["spec"]["ingress"]
        ports = [p.get("port") for entry in ingress for p in entry.get("ports", [])]
        self.assertIn(20257, ports)

        vpa = next(
            r
            for r in self.resources
            if r["kind"] == "VerticalPodAutoscaler"
            and r["metadata"]["name"] == "node-problem-detector"
        )
        self.assertEqual(vpa["spec"]["targetRef"]["kind"], "DaemonSet")
        self.assertEqual(vpa["spec"]["targetRef"]["name"], "node-problem-detector")

    def test_kernel_monitor_conditions(self) -> None:
        config = json.loads(self.config_data["kernel-monitor.json"])
        cond_types = {c["type"] for c in config["conditions"]}
        self.assertIn("KernelDeadlock", cond_types)
        self.assertIn("ReadonlyFilesystem", cond_types)

    def test_gpu_xid_monitor_critical_xids(self) -> None:
        config = json.loads(self.config_data["gpu-xid-monitor.json"])
        cond_types = {c["type"] for c in config["conditions"]}
        self.assertIn("GPUProblem", cond_types)

        critical_xids = [48, 54, 62, 64, 74, 79, 92, 95, 119, 120]
        rule = config["rules"][0]
        pattern = rule["pattern"]
        for xid in critical_xids:
            sample_line = (
                f"kernel: [1234.56] NVRM: Xid (PCI:0000:01:00.0): {xid}, critical error details"
            )
            self.assertIsNotNone(
                re.match(pattern, sample_line),
                f"Pattern must match critical GPU Xid {xid}",
            )

    def test_mount_health_custom_plugin(self) -> None:
        config = json.loads(self.config_data["mount-health-monitor.json"])
        cond_types = {c["type"] for c in config["conditions"]}
        self.assertIn("MountHung", cond_types)
        self.assertIn("check-mount-health.sh", self.config_data)

    def test_systemd_restart_monitors(self) -> None:
        config = json.loads(self.config_data["systemd-monitor.json"])
        cond_types = {c["type"] for c in config["conditions"]}
        self.assertIn("FrequentKubeletRestart", cond_types)
        self.assertIn("FrequentContainerdRestart", cond_types)

    def test_synthetic_self_test_replay(self) -> None:
        """Verify the test monitor correctly parses synthetic Xid and read-only lines for NPDSelfTest."""
        config = json.loads(self.config_data["test-monitor.json"])
        cond_types = {c["type"] for c in config["conditions"]}
        self.assertIn("NPDSelfTest", cond_types)

        xid_rule = next(r for r in config["rules"] if r["reason"] == "SelfTestGPUXidDetected")
        ro_rule = next(
            r for r in config["rules"] if r["reason"] == "SelfTestReadonlyFilesystemDetected"
        )

        sample_xid_line = "NVRM: Xid (PCI:0000:01:00.0): 79, GPU has fallen off the bus."
        sample_ro_line = "Remounting filesystem read-only due to hardware I/O error."
        sample_ext4_line = "EXT4-fs error (device nvme0n1p1): ext4_lookup: deleted inode referenced"

        self.assertIsNotNone(re.match(xid_rule["pattern"], sample_xid_line))
        self.assertIsNotNone(re.match(ro_rule["pattern"], sample_ro_line))
        self.assertIsNotNone(re.match(ro_rule["pattern"], sample_ext4_line))

    def test_repair_cleaner_dry_run_remediation(self) -> None:
        """Assert that the repair rule matches candidate nodes and enforces dry-run remediation only."""
        cleaner = next(
            r
            for r in self.resources
            if r["kind"] == "Cleaner" and r["metadata"]["name"] == "node-problem-repair"
        )
        # Spec action MUST be Scan for dry-run by default
        self.assertEqual(
            cleaner["spec"]["action"],
            "Scan",
            "Remediation Cleaner must default to dry-run (action: Scan)",
        )

        selectors = cleaner["spec"]["resourcePolicySet"]["resourceSelectors"]
        node_selector = next(s for s in selectors if s["kind"] == "Node")
        lua_eval = node_selector["evaluate"]

        # Ensure all required problem conditions are evaluated
        for cond in [
            "KernelDeadlock",
            "ReadonlyFilesystem",
            "GPUProblem",
            "MountHung",
            "FrequentKubeletRestart",
            "FrequentContainerdRestart",
            "NPDSelfTest",
        ]:
            self.assertIn(f"{cond} = true", lua_eval)

        # Ensure threshold timing is evaluated
        self.assertIn("thresholdSeconds = 300", lua_eval)
        self.assertIn("parseTimestamp", lua_eval)


if __name__ == "__main__":
    unittest.main()
