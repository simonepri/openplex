"""Verify local routers enroll with the roles required by Headscale policy."""

from __future__ import annotations

import re
import shlex
import sys
import unittest
from pathlib import Path

import yaml


class LocalRouterTest(unittest.TestCase):
    def test_router_enrollment_uses_key_roles_and_clears_persisted_tag_requests(self) -> None:
        for manifest, egress in zip(sys.argv[1:], (False, True), strict=True):
            with self.subTest(egress=egress):
                documents = yaml.safe_load_all(Path(manifest).read_text(encoding="utf-8"))
                deployment = next(
                    document for document in documents if document["kind"] == "Deployment"
                )
                containers = deployment["spec"]["template"]["spec"]["containers"]
                router = next(
                    container for container in containers if container["name"] == "tailscaled"
                )
                environment = {item["name"]: item.get("value") for item in router["env"]}
                arguments = dict(
                    argument.split("=", 1) for argument in shlex.split(environment["TS_EXTRA_ARGS"])
                )
                assert arguments["--advertise-tags"] == ""
                assert arguments.get("--accept-routes") == ("true" if egress else None)
                authentication = next(
                    item for item in router["env"] if item["name"] == "TS_AUTHKEY"
                )
                assert authentication["valueFrom"]["secretKeyRef"] == {
                    "name": "headscale-preauth",
                    "key": "authkey",
                }

    def test_private_egress_admission_accepts_key_enrollment_and_rejects_disabled_routes(
        self,
    ) -> None:
        documents = list(yaml.safe_load_all(Path(sys.argv[2]).read_text(encoding="utf-8")))
        deployment = next(document for document in documents if document["kind"] == "Deployment")
        router = deployment["spec"]["template"]["spec"]["containers"][0]
        arguments = next(item["value"] for item in router["env"] if item["name"] == "TS_EXTRA_ARGS")
        policy = next(document for document in documents if document["kind"] == "ClusterPolicy")
        policy_wave = int(policy["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
        router_wave = int(
            deployment["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0")
        )
        assert policy_wave < router_wave
        rule = next(
            rule
            for rule in policy["spec"]["rules"]
            if rule["name"] == "require-reviewed-tailscale-router-private-egress-extension"
        )
        condition = next(
            condition
            for condition in rule["validate"]["deny"]["conditions"]["any"]
            if "starts_with(" in condition["key"]
        )
        prefix = re.search(r", '([^']+)'\)", condition["key"])
        assert prefix is not None
        assert condition["operator"] == "NotEquals"
        assert condition["value"] is True
        assert arguments.startswith(prefix[1])
        assert not arguments.replace("--accept-routes=true", "--accept-routes=false").startswith(
            prefix[1]
        )
        assert not ("--advertise-tags=tag:subnet-router,tag:k8s-egress " + arguments).startswith(
            prefix[1]
        )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
