"""Verify generated scanner configuration satisfies restricted Pod Security admission."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import yaml


class ScanSecurityTest(unittest.TestCase):
    def test_child_scan_jobs_receive_non_root_context_and_writable_temporary_volumes(self) -> None:
        path = Path(sys.argv[1])
        resources = [
            r
            for r in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if r and isinstance(r, dict)
        ]
        config_maps = [
            item["data"]
            for item in resources
            if item.get("kind") == "ConfigMap"
            and "scanJob.podTemplatePodSecurityContext" in (item.get("data") or {})
        ]
        if not config_maps:
            summaries = "\n".join(
                f"  - {r.get('kind')}/{r.get('metadata', {}).get('name')}" for r in resources
            )
            raw = path.read_text(encoding="utf-8")
            cms = {
                r.get("metadata", {}).get("name"): list((r.get("data") or {}).keys())
                for r in resources
                if r.get("kind") == "ConfigMap"
            }
            self.fail(
                f"No ConfigMap found with scanJob.podTemplatePodSecurityContext in {path}.\n"
                f"File size: {path.stat().st_size} bytes (loaded {len(resources)} resources):\n{summaries}\n"
                f"ConfigMaps and keys: {cms}\n"
                f"File tail:\n{raw[-500:]}"
            )
        config = config_maps[0]
        pod = json.loads(config["scanJob.podTemplatePodSecurityContext"])
        container = json.loads(config["scanJob.podTemplateContainerSecurityContext"])
        assert pod["runAsNonRoot"] is True
        assert pod["runAsUser"] > 0
        assert pod["runAsGroup"] == pod["fsGroup"] > 0
        assert pod["seccompProfile"] == {"type": "RuntimeDefault"}
        assert container["runAsNonRoot"] is True
        assert container["allowPrivilegeEscalation"] is False
        assert container["capabilities"]["drop"] == ["ALL"]
        assert container["privileged"] is False
        assert container["readOnlyRootFilesystem"] is True
        deployment = next(item for item in resources if item and item["kind"] == "Deployment")
        assert (
            deployment["spec"]["template"]["metadata"]["annotations"]["scan-security-context"]
            == "restricted-65532-v1"
        )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
