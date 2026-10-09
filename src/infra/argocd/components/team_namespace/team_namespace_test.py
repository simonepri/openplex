"""Verify team_namespace chart rendering for open vs member submitters."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, ClassVar, override

import yaml


def load(manifest: str) -> list[dict[str, Any]]:
    documents = yaml.safe_load_all(Path(manifest).read_text(encoding="utf-8"))
    return [document for document in documents if document]


def by_kind_and_name(documents: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (doc["kind"], doc.get("metadata", {}).get("name", "")): doc
        for doc in documents
        if "kind" in doc
    }


class TeamNamespaceTest(unittest.TestCase):
    helm: ClassVar[str]
    chart: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        tools = [Path(value).resolve() for value in " ".join(sys.argv[1:-1]).split()]
        cls.helm = str(next(path for path in tools if path.name == "helm"))
        cls.chart = Path(sys.argv[-1]).resolve().parent

    def render_chart(self, *extra_args: str) -> list[dict[str, Any]]:
        cmd = [
            self.helm,
            "template",
            "team-namespace",
            str(self.chart),
            "-f",
            str(self.chart / "lint-values.yaml"),
            *extra_args,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return [doc for doc in yaml.safe_load_all(res.stdout) if doc]

    def test_open_submitters_renders_namespace_label_and_role_and_binding(self) -> None:
        """When team.submitters is 'all', namespace is labeled and open Role/RoleBinding are rendered."""
        docs = self.render_chart("--set", "team.submitters=all")
        resources = by_kind_and_name(docs)

        # Namespace assertion
        ns_key = ("Namespace", "team-fixtureteam-workloads")
        assert ns_key in resources, "Namespace must be rendered"
        ns = resources[ns_key]
        labels = ns["metadata"].get("labels", {})
        assert labels.get("teams.openplex.io/submitters") == "all", (
            f"Expected teams.openplex.io/submitters: all, got {labels}"
        )

        # Role assertion
        role_key = ("Role", "team-open-submitter")
        assert role_key in resources, "Role team-open-submitter must be rendered"
        role = resources[role_key]
        assert role["metadata"].get("namespace") == "team-fixtureteam-workloads"
        assert role.get("rules") == [
            {"apiGroups": ["ray.io"], "resources": ["rayjobs"], "verbs": ["create", "delete"]}
        ]

        # RoleBinding assertion
        rb_key = ("RoleBinding", "team-open-submitter")
        assert rb_key in resources, "RoleBinding team-open-submitter must be rendered"
        rb = resources[rb_key]
        assert rb["metadata"].get("namespace") == "team-fixtureteam-workloads"
        assert rb.get("roleRef") == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "team-open-submitter",
        }
        assert rb.get("subjects") == [
            {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Group",
                "name": "system:authenticated",
            }
        ]

    def test_members_submitters_does_not_render_open_role_or_binding(self) -> None:
        """When team.submitters is 'members', no open label or Role/RoleBinding are rendered."""
        docs = self.render_chart("--set", "team.submitters=members")
        resources = by_kind_and_name(docs)

        # Namespace assertion
        ns_key = ("Namespace", "team-fixtureteam-workloads")
        assert ns_key in resources, "Namespace must be rendered"
        ns = resources[ns_key]
        labels = ns["metadata"].get("labels", {})
        assert "teams.openplex.io/submitters" not in labels, (
            "teams.openplex.io/submitters label must not be present when submitters=members"
        )

        # Role and RoleBinding assertion
        assert ("Role", "team-open-submitter") not in resources, (
            "Role team-open-submitter must not be rendered when submitters=members"
        )
        assert ("RoleBinding", "team-open-submitter") not in resources, (
            "RoleBinding team-open-submitter must not be rendered when submitters=members"
        )

    def test_default_submitters_does_not_render_open_role_or_binding(self) -> None:
        """By default, no open label or Role/RoleBinding are rendered."""
        docs = self.render_chart()
        resources = by_kind_and_name(docs)

        ns_key = ("Namespace", "team-fixtureteam-workloads")
        assert ns_key in resources, "Namespace must be rendered"
        ns = resources[ns_key]
        labels = ns["metadata"].get("labels", {})
        assert "teams.openplex.io/submitters" not in labels, (
            "teams.openplex.io/submitters label must not be present by default"
        )
        assert ("Role", "team-open-submitter") not in resources
        assert ("RoleBinding", "team-open-submitter") not in resources


if __name__ == "__main__":
    unittest.main(argv=sys.argv[:1])
