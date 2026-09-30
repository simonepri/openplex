"""Verify rendered team lanes preserve registered destinations and promoted image ownership."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, override

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

import yaml


class RegisteredClustersTest(unittest.TestCase):
    helm: ClassVar[str]
    chart: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        tools = [Path(value).resolve() for value in " ".join(sys.argv[1:-1]).split()]
        cls.helm = str(next(path for path in tools if path.name == "helm"))
        cls.chart = Path(sys.argv[-1]).resolve().parent

    def test_unregistered_cells_are_excluded_and_registered_endpoints_win(self) -> None:
        registrations = [
            {"name": "cell-eaws-lh1", "server": "https://192.0.2.44:6443"},
            {"name": "ctrl-eaws-lh1", "server": "https://kubernetes.default.svc"},
        ]
        result = self.render(registrations)
        assert result.returncode == 0, result.stderr
        documents = [document for document in yaml.safe_load_all(result.stdout) if document]
        cell_destinations = {
            document["spec"]["destination"]["name"]
            for document in documents
            if document["kind"] == "Application" and "name" in document["spec"]["destination"]
        }
        assert cell_destinations == {"cell-eaws-lh1"}
        registry = next(document for document in documents if document["kind"] == "ConfigMap")
        resolved = json.loads(registry["data"]["registry"])
        assert {cluster["name"]: cluster["server"] for cluster in resolved} == {
            "cell-eaws-lh1": "https://192.0.2.44:6443",
            "ctrl-eaws-lh1": "https://kubernetes.default.svc",
        }

    def test_empty_registration_list_is_rejected(self) -> None:
        result = self.render([])
        assert result.returncode != 0
        assert "registeredClusters" in result.stderr

    def test_local_deployment_repository_keeps_pullable_registry(self) -> None:
        result = self.render([
            {
                "name": "cell-eaws-lh1",
                "server": "https://192.0.2.44:6443",
                "annotations": {"ecr-registry": "localhost:4566"},
            },
            {"name": "ctrl-eaws-lh1", "server": "https://kubernetes.default.svc"},
        ])
        assert result.returncode == 0, result.stderr
        documents = [document for document in yaml.safe_load_all(result.stdout) if document]
        registry = next(document for document in documents if document["kind"] == "ConfigMap")
        resolved = json.loads(registry["data"]["registry"])
        cell = next(cluster for cluster in resolved if cluster["name"] == "cell-eaws-lh1")
        assert (
            cell["delivery"]["deploymentRepositories"]["src/examples/ray_serve"]
            == "localhost:15100/000000000000/us-east-1/src/examples/ray_serve"
        )

    def test_worker_image_promotions_do_not_restore_live_worker_arrays(self) -> None:
        result = self.render([
            {"name": "cell-eaws-lh1", "server": "https://192.0.2.44:6443"},
            {"name": "ctrl-eaws-lh1", "server": "https://kubernetes.default.svc"},
        ])
        assert result.returncode == 0, result.stderr
        documents = [document for document in yaml.safe_load_all(result.stdout) if document]
        applications = [
            document
            for document in documents
            if document["kind"] == "Application"
            and document["spec"].get("source", {}).get("path")
            == "src/examples/ray_serve/deployment"
        ]
        assert applications
        for application in applications:
            compare_options = application["metadata"]["annotations"][
                "argocd.argoproj.io/compare-options"
            ].split(",")
            assert "IncludeMutationWebhook=true" in compare_options
            assert "ServerSideDiff=true" not in compare_options
            ignores = application["spec"]["ignoreDifferences"]
            ray_service = next(rule for rule in ignores if rule["kind"] == "RayService")
            assert ray_service["jsonPointers"] == ["/metadata/generation"]
            assert not any("workerGroupSpecs" in path for path in ray_service["jqPathExpressions"])

    def test_registered_cluster_without_contract_is_rejected(self) -> None:
        result = self.render([{"name": "cell-unknown", "server": "https://192.0.2.55:6443"}])
        assert result.returncode != 0
        assert 'registered cluster "cell-unknown" requires a clusterRegistry entry' in result.stderr

    def test_logical_repository_identity_preserves_local_workspace_transport(self) -> None:
        repository = {
            "url": "http://192.0.2.21:9419/cgi-bin/git/repo.git",
            "transportURL": "git://192.0.2.21:9418/repo.git",
        }
        result = self.render(
            [
                {"name": "cell-eaws-lh1", "server": "https://192.0.2.44:6443"},
                {"name": "ctrl-eaws-lh1", "server": "https://kubernetes.default.svc"},
            ],
            repository=repository,
        )
        assert result.returncode == 0, result.stderr
        applications = [
            document
            for document in yaml.safe_load_all(result.stdout)
            if document and document["kind"] == "Application"
        ]
        assert applications
        for application in applications:
            assert application["spec"]["source"]["repoURL"] == repository["url"]
        namespaces = [
            application["spec"]["source"]["helm"]["valuesObject"]
            for application in applications
            if application["spec"]["source"]["path"]
            == "src/infra/argocd/components/team_namespace/helm"
            and application["spec"]["source"]["helm"]["valuesObject"]["devWorkspaces"]["enabled"]
        ]
        assert namespaces
        for namespace in namespaces:
            assert namespace["devWorkspaces"]["localGitCIDR"] == "192.0.2.21/32"

    def test_dynamic_cluster_annotations_override_aws_account_and_ecr_registry(self) -> None:
        registrations: list[dict[str, Any]] = [
            {
                "name": "cell-aws-usw2",
                "server": "https://192.0.2.44:6443",
                "annotations": {
                    "aws-account-id": "123456789012",
                    "ecr-registry": "123456789012.dkr.ecr.us-west-2.amazonaws.com",
                },
            },
            {
                "name": "cell-gcp-euw4",
                "server": "https://192.0.2.45:6443",
                "annotations": {
                    "gcp-project-id": "project-gcp-euw4",
                    "gcp-project-number": "987654321012",
                },
            },
        ]
        result = self.render(
            registrations,
            extra_values={
                "delivery": {
                    "originRepositories": {
                        "src/examples/batch_cron": "123456789012.dkr.ecr.us-west-2.amazonaws.com/src/examples/batch_cron",
                        "src/examples/ray_data": "123456789012.dkr.ecr.us-west-2.amazonaws.com/src/examples/ray_data",
                        "src/examples/ray_serve": "123456789012.dkr.ecr.us-west-2.amazonaws.com/src/examples/ray_serve",
                        "src/examples/ray_train": "123456789012.dkr.ecr.us-west-2.amazonaws.com/src/examples/ray_train",
                        "src/examples/svelte_web": "123456789012.dkr.ecr.us-west-2.amazonaws.com/src/examples/svelte_web",
                    },
                },
            },
        )
        assert result.returncode == 0, result.stderr
        documents = [document for document in yaml.safe_load_all(result.stdout) if document]
        registry = next(document for document in documents if document["kind"] == "ConfigMap")
        resolved = json.loads(registry["data"]["registry"])
        cell = next(c for c in resolved if c["name"] == "cell-aws-usw2")
        assert (
            cell["coderProvisioner"]["identity"]["roleArn"]
            == "arn:aws:iam::123456789012:role/cell-aws-usw2-coder-provisioner"
        )
        assert (
            cell["workloadImagePush"]["roles"]["examples"]
            == "arn:aws:iam::123456789012:role/cell-aws-usw2-examples-workspace-ecr"
        )
        assert (
            cell["delivery"]["deploymentRepositories"]["src/examples/batch_cron"]
            == "123456789012.dkr.ecr.us-west-2.amazonaws.com/src/examples/batch_cron"
        )
        gcp_cell = next(c for c in resolved if c["name"] == "cell-gcp-euw4")
        assert (
            gcp_cell["coderProvisioner"]["federation"]["tokenAudience"]
            == "//iam.googleapis.com/projects/987654321012/locations/global/workloadIdentityPools/coder-provisioner/providers/active-ctrl"
        )
        assert gcp_cell["providerConfig"]["gcp"]["projectID"] == "project-gcp-euw4"
        assert (
            gcp_cell["coderProvisioner"]["identity"]["serviceAccount"]
            == "coder-provisioner@project-gcp-euw4.iam.gserviceaccount.com"
        )
        assert (
            gcp_cell["storage"]["teams"]["handoffs"]["examples"]["secretStore"]["auth"]["gcp"][
                "projectId"
            ]
            == "project-gcp-euw4"
        )
        assert (
            gcp_cell["storage"]["teams"]["handoffs"]["examples"]["secretStore"]["auth"]["gcp"][
                "serviceAccountEmail"
            ]
            == "examples-record-reader@project-gcp-euw4.iam.gserviceaccount.com"
        )

    def render(
        self,
        registrations: Sequence[Mapping[str, Any]],
        *,
        repository: dict[str, str] | None = None,
        extra_values: dict[str, Any] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "chart"
            shutil.copytree(self.chart, chart)
            (chart / "templates/registry-probe.yaml").write_text(
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: registry-probe\n"
                'data:\n  registry: {{ include "fleet-team.registeredClusters" . | quote }}\n',
                encoding="utf-8",
            )
            values = Path(directory) / "registrations.json"
            overrides: dict[str, object] = {"registeredClusters": registrations}
            if repository is not None:
                overrides["repository"] = repository
            if extra_values:
                overrides.update(extra_values)
            values.write_text(json.dumps(overrides), encoding="utf-8")
            return subprocess.run(
                [
                    self.helm,
                    "template",
                    "team",
                    str(chart),
                    "-f",
                    str(chart / "lint-values.yaml"),
                    "-f",
                    str(values),
                ],
                check=False,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
