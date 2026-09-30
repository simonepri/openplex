"""Exercise bootstrap Ray health checks with Argo CD's actual Lua evaluator."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import hcl2


class HealthTest(unittest.TestCase):
    def test_ray_readiness_and_generation_gate_health(self) -> None:
        with Path(sys.argv[2]).open(encoding="utf-8") as source:
            scripts = hcl2.load(source)["locals"][0]["ray_health"]
        ready_statuses: dict[str, dict[str, Any]] = {
            "RayService": {
                "observedGeneration": 2,
                "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 2}],
            },
            "RayCluster": {
                "observedGeneration": 2,
                "readyWorkerReplicas": 1,
                "desiredWorkerReplicas": 1,
                "conditions": [
                    {"type": "HeadPodReady", "status": "True"},
                    {"type": "RayClusterProvisioned", "status": "True"},
                ],
            },
        }
        for kind, ready in ready_statuses.items():
            stale = {**ready, "observedGeneration": 1}
            unready = copy.deepcopy(ready)
            unready["conditions"][0]["status"] = "False"
            cases = [({}, "Progressing"), (ready, "Healthy"), (stale, "Progressing")]
            cases.append((unready, "Progressing"))
            if kind == "RayCluster":
                cases.extend([
                    ({**ready, "readyWorkerReplicas": 0}, "Progressing"),
                    ({**ready, "readyWorkerReplicas": 0, "desiredWorkerReplicas": 0}, "Healthy"),
                ])
                crashed = copy.deepcopy(unready)
                crashed["conditions"][0]["reason"] = "CrashLoopBackOff"
                cases.append((crashed, "Degraded"))
            else:
                stale_condition = copy.deepcopy(ready)
                stale_condition["conditions"][0]["observedGeneration"] = 1
                cases.append((stale_condition, "Progressing"))
            for status, expected in cases:
                with self.subTest(kind=kind, status=status):
                    script = "\n".join(scripts[kind].splitlines()[1:-1])
                    result = evaluate(script, kind, status)
                    assert result.returncode == 0, result.stderr
                    assert f"STATUS: {expected}\n" in result.stdout, result.stdout


def evaluate(script: str, kind: str, status: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        resource = root / "resource.json"
        config = root / "config.json"
        secret = root / "secret.json"
        resource.write_text(
            json.dumps({
                "apiVersion": "ray.io/v1",
                "kind": kind,
                "metadata": {"name": "test", "generation": 2},
                "status": status,
            })
        )
        config.write_text(
            json.dumps({
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "argocd-cm"},
                "data": {f"resource.customizations.health.ray.io_{kind}": script},
            })
        )
        secret.write_text(
            json.dumps({
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "argocd-secret"},
            })
        )
        return subprocess.run(
            [
                sys.argv[1],
                "admin",
                "settings",
                "resource-overrides",
                "health",
                str(resource),
                "--argocd-cm-path",
                str(config),
                "--argocd-secret-path",
                str(secret),
            ],
            capture_output=True,
            text=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main(argv=sys.argv[:1])
