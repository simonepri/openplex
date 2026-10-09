"""Verify team-open-submitter ValidatingAdmissionPolicy rules for RayJob access."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

import yaml


def load(manifest: str) -> list[dict[str, Any]]:
    documents = yaml.safe_load_all(Path(manifest).read_text(encoding="utf-8"))
    return [document for document in documents if document]


def policies(documents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "ValidatingAdmissionPolicy"
    }


def policy_bindings(documents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "ValidatingAdmissionPolicyBinding"
    }


def simulate_admission(username: str, patch_allowed: bool) -> bool:
    """Simulate the CEL expression evaluated by the ValidatingAdmissionPolicy."""
    return (
        username.startswith(("cluster:user:", "system:serviceaccount:kube-system:"))
        or patch_allowed
    )


class TeamOpenSubmitterPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        docs = load(sys.argv[1])
        pols = policies(docs)
        bindings = policy_bindings(docs)
        assert "team-open-submitter" in pols, "team-open-submitter policy not found"
        assert "team-open-submitter" in bindings, "team-open-submitter binding not found"
        cls.policy = pols["team-open-submitter"]
        cls.binding = bindings["team-open-submitter"]

    def test_policy_match_constraints(self) -> None:
        """Verify policy matches CREATE and DELETE on ray.io/v1 rayjobs."""
        spec = self.policy.get("spec", {})
        assert spec.get("failurePolicy") == "Fail"

        match_constraints = spec.get("matchConstraints", {})
        rules = match_constraints.get("resourceRules", [])
        assert len(rules) == 1, f"Expected 1 resourceRule, got {len(rules)}"
        rule = rules[0]
        assert rule.get("apiGroups") == ["ray.io"]
        assert rule.get("apiVersions") == ["v1"]
        assert sorted(rule.get("operations", [])) == ["CREATE", "DELETE"]
        assert rule.get("resources") == ["rayjobs"]

    def test_policy_validations(self) -> None:
        """Verify CEL expression allows humans, kube-system SAs, and authorized patchers."""
        spec = self.policy.get("spec", {})
        validations = spec.get("validations", [])
        assert len(validations) == 1, f"Expected 1 validation rule, got {len(validations)}"
        val = validations[0]
        expr = val.get("expression", "")
        message = val.get("message", "")

        assert "request.userInfo.username.startsWith('cluster:user:')" in expr
        assert "request.userInfo.username.startsWith('system:serviceaccount:kube-system:')" in expr
        assert (
            "authorizer.group('ray.io').resource('rayjobs').namespace(request.namespace).check('patch').allowed()"
            in expr
        )
        assert "open-submission" in message or "RayJob" in message

    def test_binding_match_resources(self) -> None:
        """Verify binding selects namespaces labeled with teams.openplex.io/submitters: all."""
        spec = self.binding.get("spec", {})
        assert spec.get("policyName") == "team-open-submitter"
        assert spec.get("validationActions") == ["Deny"]

        match_resources = spec.get("matchResources", {})
        ns_selector = match_resources.get("namespaceSelector", {})
        match_labels = ns_selector.get("matchLabels", {})
        assert match_labels.get("teams.openplex.io/submitters") == "all"

    def test_admission_decision_simulation(self) -> None:
        """Verify CEL decision logic: allows humans, kube-system SAs, patch-holders, rejects others."""
        # 1. Authenticated human via OIDC proxy (allowed)
        assert simulate_admission("cluster:user:alice@openplex.io", patch_allowed=False)
        assert simulate_admission("cluster:user:bob@openplex.io", patch_allowed=False)

        # 2. Core controllers in kube-system (allowed)
        assert simulate_admission(
            "system:serviceaccount:kube-system:generic-garbage-collector", patch_allowed=False
        )
        assert simulate_admission(
            "system:serviceaccount:kube-system:job-controller", patch_allowed=False
        )

        # 3. Principals with broader patch permission (e.g. team members, Argo CD) (allowed)
        assert simulate_admission(
            "system:serviceaccount:argocd:argocd-application-controller", patch_allowed=True
        )
        assert simulate_admission(
            "system:serviceaccount:kuberay:kuberay-operator", patch_allowed=True
        )

        # 4. Untrusted service accounts without patch permission (denied - rejection path)
        assert not simulate_admission("system:serviceaccount:default:attacker", patch_allowed=False)
        assert not simulate_admission(
            "system:serviceaccount:team-examples-workloads:workload-sa", patch_allowed=False
        )
        assert not simulate_admission(
            "system:serviceaccount:workspaces:coder-workspace", patch_allowed=False
        )

        # 5. Unauthenticated or generic authenticated SA without prefix (denied)
        assert not simulate_admission("system:unauthenticated", patch_allowed=False)
        assert not simulate_admission("system:anonymous", patch_allowed=False)


if __name__ == "__main__":
    unittest.main(argv=sys.argv[:1])
