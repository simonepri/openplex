"""Verify Cleaner corrupt-image-node-cleaner conforms to CRD schema and Lua contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, ClassVar, NamedTuple, override

import jsonschema
import yaml


def _to_lua(val: object) -> str:
    if val is None:
        return "nil"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return json.dumps(val)
    if isinstance(val, list):
        return "{" + ", ".join(_to_lua(x) for x in val) + "}"
    if isinstance(val, dict):
        entries = [f"[{json.dumps(str(k))}] = {_to_lua(v)}" for k, v in val.items()]
        return "{" + ", ".join(entries) + "}"
    raise TypeError(f"Unsupported type {type(val)}")


def _find_lua_binary() -> str | None:
    candidates = [
        shutil.which("lua"),
        shutil.which("luajit"),
        "/opt/homebrew/bin/lua",
        "/usr/local/bin/lua",
        "/usr/bin/lua",
    ]
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


class TestCase(NamedTuple):
    name: str
    description: str
    resources: list[dict[str, Any]]
    expected_flagged_nodes: list[str]
    expected_message_contains: list[str]


class CorruptImageCleanerTest(unittest.TestCase):
    helm: ClassVar[str]
    cleaner_crd_schema: ClassVar[dict[str, Any]]
    cleaner_manifest: ClassVar[dict[str, Any]]
    lua_binary: ClassVar[str | None]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        tools = [Path(value).resolve() for value in " ".join(sys.argv[1:-2]).split()]
        CorruptImageCleanerTest.helm = str(next(path for path in tools if path.name == "helm"))
        chart_pkg = Path(sys.argv[-2]).resolve()
        cleaner_file = Path(sys.argv[-1]).resolve()

        CorruptImageCleanerTest.cleaner_manifest = yaml.safe_load(
            cleaner_file.read_text(encoding="utf-8")
        )
        CorruptImageCleanerTest.lua_binary = _find_lua_binary()

        result = subprocess.run(
            [
                CorruptImageCleanerTest.helm,
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
        CorruptImageCleanerTest.cleaner_crd_schema = cleaner_crd["spec"]["versions"][0]["schema"][
            "openAPIV3Schema"
        ]

    def test_cleaner_manifest_conforms_to_crd_schema(self) -> None:
        spec_schema = self.cleaner_crd_schema["properties"]["spec"]
        spec = self.cleaner_manifest["spec"]
        jsonschema.validate(instance=spec, schema=spec_schema)
        for key in spec:
            assert key in spec_schema["properties"], f"Undeclared key {key} in Cleaner spec"

    def test_cleaner_action_is_transform(self) -> None:
        assert self.cleaner_manifest["spec"]["action"] == "Transform"

    def test_cleaner_selects_pods_and_nodes(self) -> None:
        selectors = self.cleaner_manifest["spec"]["resourcePolicySet"]["resourceSelectors"]
        kinds = {s["kind"] for s in selectors}
        assert "Pod" in kinds, "Expected resourceSelectors to include Pod"
        assert "Node" in kinds, "Expected resourceSelectors to include Node"

    def test_cleaner_has_event_notification(self) -> None:
        notifications = self.cleaner_manifest["spec"].get("notifications", [])
        types = {n.get("type") for n in notifications}
        assert "Event" in types, "Expected notification of type Event"

    def test_transform_contract_taints_node_in_live_mode_and_preserves_in_dry_run(self) -> None:
        transform = self.cleaner_manifest["spec"]["transform"]
        assert "function transform()" in transform
        assert "node.kubernetes.io/corrupt-image" in transform
        assert "NoSchedule" in transform
        assert "[DRY-RUN]" in transform

        if self.lua_binary:
            driver_dry_run = f"""
obj = {{ kind = "Node", metadata = {{ name = "node-1" }}, spec = {{ taints = {{}} }} }}
{transform}
local hs = transform()
assert(hs.resource ~= nil, "hs.resource required")
assert(#hs.resource.spec.taints == 0, "dry-run must not mutate taints")
assert(string.find(hs.message, "DRY%-RUN") ~= nil, "dry-run message required")
print("DRY_RUN_OK")
"""
            p1 = subprocess.run(
                [self.lua_binary, "-e", driver_dry_run],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p1.returncode == 0, f"Lua dry-run transform failed: {p1.stderr}"
            assert "DRY_RUN_OK" in p1.stdout

            driver_live = f"""
obj = {{
  kind = "Node",
  metadata = {{ name = "node-1", annotations = {{ ["k8s-cleaner.io/dry-run"] = "false" }} }},
  spec = {{ taints = {{}} }}
}}
{transform}
local hs = transform()
assert(hs.resource ~= nil, "hs.resource required")
assert(#hs.resource.spec.taints == 1, "live mode must insert taint")
assert(hs.resource.spec.taints[1].key == "node.kubernetes.io/corrupt-image")
assert(hs.resource.spec.taints[1].effect == "NoSchedule")
print("LIVE_OK")
"""
            p2 = subprocess.run(
                [self.lua_binary, "-e", driver_live],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p2.returncode == 0, f"Lua live transform failed: {p2.stderr}"
            assert "LIVE_OK" in p2.stdout

    def test_table_driven_lua_evaluation(self) -> None:
        """Table-driven unit tests for Lua logic across anomaly, failure, and healthy cases."""
        lua_code = self.cleaner_manifest["spec"]["resourcePolicySet"]["aggregatedSelection"]

        test_cases = [
            TestCase(
                name="corrupt_on_one_node",
                description=(
                    "Pod crashloops on node-1 with >=3 restarts while the same digest runs "
                    "healthy on node-2 -> node-1 flagged"
                ),
                resources=[
                    {
                        "kind": "Node",
                        "metadata": {
                            "name": "node-1",
                            "labels": {"karpenter.sh/nodepool": "default"},
                            "annotations": {"karpenter.sh/nodeclaim": "claim-xyz"},
                        },
                    },
                    {
                        "kind": "Node",
                        "metadata": {
                            "name": "node-2",
                            "labels": {"eks.amazonaws.com/nodegroup": "ng-workers"},
                        },
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-crashing-1"},
                        "spec": {"nodeName": "node-1"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "image": "app:v1",
                                    "imageID": "repo/app@sha256:corruptdigest123",
                                    "restartCount": 4,
                                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                }
                            ],
                        },
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-healthy-2"},
                        "spec": {"nodeName": "node-2"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "image": "app:v1",
                                    "imageID": "repo/app@sha256:corruptdigest123",
                                    "restartCount": 0,
                                    "ready": True,
                                    "state": {"running": {}},
                                }
                            ],
                        },
                    },
                ],
                expected_flagged_nodes=["node-1"],
                expected_message_contains=[
                    "[DRY-RUN]",
                    "node.kubernetes.io/corrupt-image=true:NoSchedule",
                    "delete Karpenter NodeClaim claim-xyz",
                    "pod-crashing-1",
                ],
            ),
            TestCase(
                name="crashing_on_every_node",
                description=(
                    "Pod crashloops on every node (0 healthy replicas elsewhere) "
                    "-> cluster-wide failure, no node flagged"
                ),
                resources=[
                    {
                        "kind": "Node",
                        "metadata": {"name": "node-1"},
                    },
                    {
                        "kind": "Node",
                        "metadata": {"name": "node-2"},
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-crashing-1"},
                        "spec": {"nodeName": "node-1"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "image": "app:v1",
                                    "imageID": "repo/app@sha256:brokenbuild456",
                                    "restartCount": 5,
                                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                }
                            ],
                        },
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-crashing-2"},
                        "spec": {"nodeName": "node-2"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "image": "app:v1",
                                    "imageID": "repo/app@sha256:brokenbuild456",
                                    "restartCount": 3,
                                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                }
                            ],
                        },
                    },
                ],
                expected_flagged_nodes=[],
                expected_message_contains=["No corrupt image anomalies detected"],
            ),
            TestCase(
                name="healthy",
                description="All pods running healthy across all nodes -> no nodes flagged",
                resources=[
                    {
                        "kind": "Node",
                        "metadata": {"name": "node-1"},
                    },
                    {
                        "kind": "Node",
                        "metadata": {"name": "node-2"},
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-healthy-1"},
                        "spec": {"nodeName": "node-1"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "image": "app:v1",
                                    "imageID": "repo/app@sha256:healthyimage789",
                                    "restartCount": 0,
                                    "ready": True,
                                    "state": {"running": {}},
                                }
                            ],
                        },
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-healthy-2"},
                        "spec": {"nodeName": "node-2"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "image": "app:v1",
                                    "imageID": "repo/app@sha256:healthyimage789",
                                    "restartCount": 0,
                                    "ready": True,
                                    "state": {"running": {}},
                                }
                            ],
                        },
                    },
                ],
                expected_flagged_nodes=[],
                expected_message_contains=["No corrupt image anomalies detected"],
            ),
            TestCase(
                name="restarts_below_threshold",
                description=(
                    "Pod has only 2 restarts on node-1 while running on node-2 -> below threshold, not flagged"
                ),
                resources=[
                    {
                        "kind": "Node",
                        "metadata": {"name": "node-1"},
                    },
                    {
                        "kind": "Node",
                        "metadata": {"name": "node-2"},
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-1"},
                        "spec": {"nodeName": "node-1"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "imageID": "repo/app@sha256:test123",
                                    "restartCount": 2,
                                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                }
                            ],
                        },
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-2"},
                        "spec": {"nodeName": "node-2"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "imageID": "repo/app@sha256:test123",
                                    "restartCount": 0,
                                    "ready": True,
                                    "state": {"running": {}},
                                }
                            ],
                        },
                    },
                ],
                expected_flagged_nodes=[],
                expected_message_contains=["No corrupt image anomalies detected"],
            ),
            TestCase(
                name="managed_nodegroup_lifecycle_action",
                description=(
                    "Corrupt node is part of AWS managed nodegroup -> message informs operator to recycle"
                ),
                resources=[
                    {
                        "kind": "Node",
                        "metadata": {
                            "name": "node-managed",
                            "labels": {"eks.amazonaws.com/nodegroup": "core-ng"},
                        },
                    },
                    {
                        "kind": "Node",
                        "metadata": {"name": "node-other"},
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-ng-bad"},
                        "spec": {"nodeName": "node-managed"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "imageID": "repo/app@sha256:ngbad",
                                    "restartCount": 3,
                                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                }
                            ],
                        },
                    },
                    {
                        "kind": "Pod",
                        "metadata": {"name": "pod-ng-good"},
                        "spec": {"nodeName": "node-other"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "name": "worker",
                                    "imageID": "repo/app@sha256:ngbad",
                                    "restartCount": 0,
                                    "ready": True,
                                    "state": {"running": {}},
                                }
                            ],
                        },
                    },
                ],
                expected_flagged_nodes=["node-managed"],
                expected_message_contains=[
                    "[DRY-RUN]",
                    "inform operator to recycle managed node group instance node-managed (nodegroup: core-ng)",
                ],
            ),
        ]

        for tc in test_cases:
            with self.subTest(case=tc.name):
                if not self.lua_binary:
                    assert tc.resources
                    continue

                driver = f"""
resources = {_to_lua(tc.resources)}
{lua_code}
local res = evaluate()
local flagged = {{}}
for _, r in ipairs(res.resources or {{}}) do
  local node = r.resource or r
  table.insert(flagged, node.metadata.name)
end
print("FLAGGED:" .. table.concat(flagged, ","))
print("MESSAGE:" .. (res.message or ""))
"""
                proc = subprocess.run(
                    [self.lua_binary, "-e", driver],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert proc.returncode == 0, f"Lua execution failed for {tc.name}: {proc.stderr}"

                flagged_line = next(
                    line for line in proc.stdout.splitlines() if line.startswith("FLAGGED:")
                )
                actual_flagged = [n for n in flagged_line[len("FLAGGED:") :].split(",") if n]
                assert actual_flagged == tc.expected_flagged_nodes, (
                    f"Case {tc.name}: expected flagged nodes {tc.expected_flagged_nodes}, got {actual_flagged}"
                )

                message = proc.stdout
                for exp in tc.expected_message_contains:
                    assert exp in message, (
                        f"Case {tc.name}: expected message to contain '{exp}', got: {message}"
                    )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
