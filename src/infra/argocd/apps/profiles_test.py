"""Render fleet ApplicationSet patches with both availability profiles."""

# TODO(simonepri): Extract and publish offline ApplicationSet matrix evaluation
# and validation as a standalone open-source CLI.
#
# Architectural Design:
# 1. Phase 1 (AppSet Matrix Evaluator): Emulate the Argo CD ApplicationSet
#    controller's generator matrix (clusters, git, list) and Go template
#    rendering completely offline using local YAML/JSON context without requiring
#    a running Kubernetes cluster.
# 2. Phase 2 (Child Manifest Inflator): Parse the resulting Application CRs,
#    resolve local Helm charts and Kustomizations, and invoke `helm template` /
#    `kustomize build` using the merged valuesObject and valueFiles.
# 3. Phase 3 (Static Lint & Schema Engine): Validate values against Helm
#    values.schema.json, enforce Kubernetes OpenAPI list-map key uniqueness
#    (preventing duplicate-env-var rejections in Server-Side Apply), and run
#    pluggable manifest linters (kube-linter, kubeconform, PDB availability)
#    in pre-commit and CI gates.

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, override

import yaml


class UniqueKeyLoader(yaml.SafeLoader):
    @override
    def construct_mapping(self, node: yaml.MappingNode, deep: Any = False) -> dict[Any, Any]:
        keys = [self.construct_object(key, deep=bool(deep)) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("rendered YAML contains duplicate mapping keys")
        return super().construct_mapping(node, deep=bool(deep))


EXPECTED_CUSTOM_REPLICAS = 3
EXPECTED_KARPENTER_REPLICAS = 2


def _assert_unique_names(node: object, path: str = "") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _assert_unique_names(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        names = [item["name"] for item in node if isinstance(item, dict) and "name" in item]
        if len(names) != len(set(names)):
            dups = {n for n in names if names.count(n) > 1}
            msg = f"Duplicate 'name' keys {dups} found at {path}"
            raise AssertionError(msg)
        for i, item in enumerate(node):
            _assert_unique_names(item, f"{path}[{i}]")


def _verify_patches_have_unique_names(patches: list[dict[str, Any]]) -> None:
    for patch in patches:
        for source in patch["spec"]["sources"]:
            helm = source.get("helm")
            if helm and "valuesObject" in helm:
                _assert_unique_names(
                    helm["valuesObject"],
                    f"{patch['metadata']['name']}.helm.valuesObject",
                )


class _ProfilesBaseTest(unittest.TestCase):
    __test__ = False

    helm: str
    kustomize: str
    clickhouse_base: str
    dragonfly_client_image: dict[str, Any]
    dragonfly_inventory: dict[str, Any]
    infrastructure_inventory: dict[str, Any]
    registry: dict[str, Any]
    apps: list[dict[str, Any]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        tool_files = [Path(arg).resolve() for arg in " ".join(sys.argv[1:-3]).split()]
        cls.helm = str(next(path for path in tool_files if path.name == "helm"))
        cls.kustomize = str(next(path for path in tool_files if path.name == "kustomize"))
        cls.clickhouse_base = next(
            path
            for path in tool_files
            if path.name == "base_render.yaml" and path.parent.name == "clickhouse"
        ).read_text()
        cls.dragonfly_client_image = yaml.safe_load(
            next(path for path in tool_files if path.name == "client-image.yaml").read_text()
        )
        cls.infrastructure_inventory = json.loads(
            next(
                path for path in tool_files if path.name == "infrastructure-images.json"
            ).read_text()
        )
        cls.dragonfly_inventory = next(
            entry
            for entry in cls.infrastructure_inventory["images"]
            if entry["source"] == "src/third_party/dragonflyoss/client"
        )
        cls.registry = yaml.safe_load(Path(sys.argv[-3]).read_text(encoding="utf-8"))
        cls.apps = [yaml.safe_load(Path(arg).read_text(encoding="utf-8")) for arg in sys.argv[-2:]]

    def parameters(self, component: dict[str, Any], profile: str) -> dict[str, Any]:
        return {
            **copy.deepcopy(self.registry),
            **component,
            "nameNormalized": "test-cluster",
            "metadata": {
                "annotations": {
                    "backups-bucket": "foundation-test-backups",
                    "git-repo-url": "https://github.com/example/repo.git",
                    "s3-endpoint": "http://172.19.0.2:4566",
                    "secret-store": "runtime-secrets",
                    "registered-cells": "cell-eaws-lh1",
                },
                "labels": {"provider": "floci", "profile": profile},
            },
        }

    def render(
        self, app: dict[str, Any], parameters: list[dict[str, Any]]
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory)
            (chart / "templates").mkdir()
            (chart / "Chart.yaml").write_text(
                "apiVersion: v2\nname: profile-test\nversion: 0.1.0\n"
            )
            (chart / "values.yaml").write_text(json.dumps({"cases": parameters}))
            template = (
                "{{ range .Values.cases }}\n---\n"
                "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n"
                "  name: {{ .component }}\n"
            )
            (chart / "templates/patch.yaml").write_text(
                template + app["spec"]["templatePatch"] + "\n{{ end }}\n"
            )
            return subprocess.run(
                [self.helm, "template", "profile-test", str(chart)],
                capture_output=True,
                text=True,
                check=False,
            )


class ProfilesAppTest(_ProfilesBaseTest):
    def test_dragonfly_wait_containers_do_not_reserve_server_sized_resources(self) -> None:
        inputs = [Path(arg) for arg in " ".join(sys.argv[1:-3]).split()]
        for component, wait_container in (
            ("dragonfly_manager", "wait-for-postgres"),
            ("dragonfly_peer", "wait-for-scheduler"),
        ):
            with self.subTest(component=component):
                rendered = next(
                    path
                    for path in inputs
                    if path.parent.name == component and path.name == "vendor-helm-render.yaml"
                )
                containers = [
                    container
                    for resource in yaml.safe_load_all(rendered.read_text())
                    if resource and resource["kind"] in {"Deployment", "StatefulSet"}
                    for container in resource["spec"]["template"]["spec"].get("initContainers", [])
                    if container["name"] == wait_container
                ]
                assert len(containers) == 1
                container = containers[0]
                assert container["resources"]["requests"] == {"cpu": "10m", "memory": "32Mi"}
                assert str(container["resources"]["limits"]["cpu"]) == "2"
                assert container["resources"]["limits"]["memory"] == "4Gi"
                assert "busybox" in container["image"]
                assert "nc -vz" in container["command"][-1]

    def test_vpa_profiles_preserve_history_and_enable_in_place_updates(self) -> None:
        inputs = [Path(arg) for arg in " ".join(sys.argv[1:-3]).split()]
        values = next(
            path
            for path in inputs
            if path.name == "values.yaml" and path.parent.parent.name == "vpa"
        )
        chart = next(path for path in inputs if path.name.startswith("vertical-pod-autoscaler-"))
        history = yaml.safe_load(values.read_text())["recommender"]["extraArgs"]
        assert history == [
            "--storage=checkpoint",
            "--cpu-histogram-decay-half-life=8h",
            "--memory-histogram-decay-half-life=8h",
            "--memory-aggregation-interval=8h",
            "--recommender-interval=1m",
            "--checkpoints-timeout=45s",
            "--v=2",
        ]
        floors = [
            "--pod-recommendation-min-memory-mb=32",
            "--pod-recommendation-min-cpu-millicores=10",
        ]
        for app in self.apps:
            components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
            component = next(item for item in components if item["component"] == "vpa")
            for profile in ("minimal", "production"):
                with self.subTest(dispatcher=app["metadata"]["name"], profile=profile):
                    rendered = self.render(app, [self.parameters(component, profile)])
                    assert rendered.returncode == 0, rendered.stderr
                    override = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"][
                        "valuesObject"
                    ]
                    with tempfile.TemporaryDirectory() as directory:
                        profile_values = Path(directory) / "profile.yaml"
                        profile_values.write_text(yaml.safe_dump(override))
                        child = subprocess.run(
                            [
                                self.helm,
                                "template",
                                "vpa",
                                str(chart),
                                "-f",
                                str(values),
                                "-f",
                                str(profile_values),
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                    assert child.returncode == 0, child.stderr
                    rendered_resources = list(yaml.safe_load_all(child.stdout))
                    crds = [
                        resource
                        for resource in rendered_resources
                        if resource and resource.get("kind") == "CustomResourceDefinition"
                    ]
                    assert not crds
                    deployments = {
                        resource["metadata"]["name"]: resource
                        for resource in rendered_resources
                        if resource and resource.get("kind") == "Deployment"
                    }
                    for controller in ("admission-controller", "updater"):
                        controller_args = deployments[f"vpa-{controller}"]["spec"]["template"][
                            "spec"
                        ]["containers"][0]["args"]
                        assert "--feature-gates=InPlace=true" in controller_args
                    updater_args = deployments["vpa-updater"]["spec"]["template"]["spec"][
                        "containers"
                    ][0]["args"]
                    settling_period = "5m" if profile == "minimal" else "1h"
                    assert [arg for arg in updater_args if "lifetime-threshold=" in arg] == [
                        "--in-recommendation-bounds-eviction-lifetime-threshold=" + settling_period
                    ]
                    assert "--pod-update-threshold=0.15" in updater_args
                    assert "--updater-interval=1m" in updater_args
                    assert "--in-place-skip-disruption-budget=true" in updater_args
                    assert "--min-replicas=1" in updater_args
                    deployment = deployments["vpa-recommender"]
                    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
                    expected_history = [
                        "--recommender-interval=5m"
                        if profile == "minimal" and arg == "--recommender-interval=1m"
                        else arg
                        for arg in history
                    ]
                    expected = [*expected_history, *floors] if profile == "minimal" else history
                    assert [
                        arg
                        for arg in args
                        if arg in expected_history or "pod-recommendation-min-" in arg
                    ] == expected
                    assert [arg for arg in args if arg.startswith("--recommender-interval=")] == [
                        "--recommender-interval=5m"
                        if profile == "minimal"
                        else "--recommender-interval=1m"
                    ]
                    assert not any("recommendation-margin-fraction" in arg for arg in args)
                    assert not any(
                        "prometheus" in arg or "history-resolution" in arg for arg in args
                    )

    def test_application_templates_reject_helm_only_functions(self) -> None:
        # The rendering harness supplies Helm helpers unavailable to ApplicationSet.
        for app in self.apps:
            with self.subTest(application=app["metadata"]["name"]):
                actions = re.findall(r"{{(.*?)}}", app["spec"].get("templatePatch", ""), re.DOTALL)
                for action in actions:
                    code = re.sub(r'"(?:\\.|[^"\\])*"|`[^`]*`', "", action)
                    assert not re.search(r"\b(?:required|include|tpl|lookup)\b", code), action

    def test_local_database_backups_use_registered_storage_and_runtime_credentials(self) -> None:
        cases = (
            ("ctrl-apps", "buildbuddy", "buildbuddy-postgres"),
            ("ctrl-apps", "coder", "coder-postgres"),
            ("ctrl-apps", "dragonfly-manager", "dragonfly-postgres"),
            ("ctrl-apps", "signoz", "signoz-postgres"),
            ("cell-apps", "buildbuddy-cache", "buildbuddy-cache-postgres"),
        )
        for app_name, component_name, database in cases:
            app = next(app for app in self.apps if app["metadata"]["name"] == app_name)
            components = [
                component
                for generator in app["spec"]["generators"]
                for component in generator["matrix"]["generators"][1]["list"]["elements"]
            ]
            component = next(item for item in components if item["component"] == component_name)
            for provider in ("aws", "floci", "gcp"):
                with self.subTest(component=component_name, provider=provider):
                    params = self.parameters(component, "minimal")
                    params["metadata"]["labels"]["provider"] = provider
                    params["metadata"]["annotations"]["backups-bucket"] = "registered-backup-bucket"
                    params["metadata"]["annotations"]["s3-endpoint"] = "http://192.0.2.25:14566"
                    if provider != "floci":
                        params["metadata"]["annotations"]["ecr-registry"] = (
                            "111122223333.dkr.ecr.us-west-2.amazonaws.com"
                        )
                    rendered = self.render(app, [params])
                    assert rendered.returncode == 0, rendered.stderr
                    sources = yaml.safe_load(rendered.stdout)["spec"]["sources"]
                    credentials = [
                        source
                        for source in sources
                        if source.get("path")
                        == "src/infra/argocd/components/local_backup_credentials/helm"
                    ]
                    patches = [
                        patch
                        for source in sources
                        for patch in source.get("kustomize", {}).get("patches", [])
                    ]
                    stores = [
                        patch for patch in patches if patch["target"].get("kind") == "ObjectStore"
                    ]
                    if provider in {"aws", "floci"}:
                        assert not credentials
                        assert len(stores) == 1
                        assert stores[0]["target"]["name"] == f"{database}-backups"
                        assert yaml.safe_load(stores[0]["patch"]) == [
                            {
                                "op": "replace",
                                "path": "/spec/configuration/destinationPath",
                                "value": "s3://registered-backup-bucket/backups/databases",
                            },
                            {
                                "op": "replace",
                                "path": "/spec/configuration/s3Credentials",
                                "value": {"inheritFromIAMRole": True},
                            },
                        ]
                        obsolete = [
                            patch
                            for patch in patches
                            if patch["target"].get("kind") == "Secret"
                            and patch["target"].get("name") == f"{database}-backup-s3"
                        ]
                        if database == "signoz-postgres":
                            assert not obsolete
                        else:
                            assert len(obsolete) == 1
                            assert yaml.safe_load(obsolete[0]["patch"])["$patch"] == "delete"
                        continue
                    assert not credentials
                    assert len(stores) == 1
                    assert stores[0]["target"]["name"] == f"{database}-backups"
                    assert yaml.safe_load(stores[0]["patch"]) == [
                        {
                            "op": "replace",
                            "path": "/spec/configuration/destinationPath",
                            "value": "gs://registered-backup-bucket/backups/databases",
                        },
                        {
                            "op": "add",
                            "path": "/spec/configuration/googleCredentials",
                            "value": {"gkeEnvironment": True},
                        },
                        {
                            "op": "remove",
                            "path": "/spec/configuration/s3Credentials",
                        },
                    ]
            for annotation in ("backups-bucket",):
                missing = self.parameters(component, "minimal")
                missing["metadata"]["annotations"].pop(annotation)
                assert self.render(app, [missing]).returncode != 0
            for provider in ("aws", "gcp"):
                missing = self.parameters(component, "production")
                missing["metadata"]["labels"]["provider"] = provider
                missing["metadata"]["annotations"].pop("backups-bucket", None)
                res = self.render(app, [missing])
                assert res.returncode != 0
                assert "database backups require the registered backups-bucket" in res.stderr

    def test_gpu_namespace_policies_require_the_gpu_operator_cluster_label(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "network-isolation")
        for provider in ("aws", "floci", "gcp"):
            for profile in ("minimal", "production"):
                for gpu in (None, "false", "true"):
                    with self.subTest(provider=provider, profile=profile, gpu=gpu):
                        params = self.parameters(component, profile)
                        params["metadata"]["labels"]["provider"] = provider
                        if gpu is not None:
                            params["metadata"]["labels"]["gpu"] = gpu
                        rendered = self.render(app, [params])
                        assert rendered.returncode == 0, rendered.stderr
                        sources = yaml.safe_load(rendered.stdout)["spec"]["sources"]
                        values = next(
                            source["helm"]["valuesObject"]
                            for source in sources
                            if source.get("path") == component["path"]
                        )
                        namespaces = values["namespaces"]
                        assert ("gpu-system" in namespaces) == (gpu == "true")
                        assert "kuberay-system" in namespaces
                        assert ("karpenter-system" in namespaces) == (provider in {"aws", "gcp"})

    def test_scale_zero_ingress_selects_backend_pods(self) -> None:
        inputs = [Path(arg) for arg in " ".join(sys.argv[1:-3]).split()]
        routing = next(path for path in inputs if path.name == "helm_render-local-cell.yaml")
        workload = next(
            path
            for path in inputs
            if path.name == "manifest.yaml" and path.parent.name == "svelte_web"
        )
        resources = list(yaml.safe_load_all(routing.read_text()))
        policy = next(
            item
            for item in resources
            if item
            and item["kind"] == "NetworkPolicy"
            and item["metadata"]["name"] == "svelte-web-keda-http"
        )
        backend = list(yaml.safe_load_all(workload.read_text()))
        service = next(item for item in backend if item["kind"] == "Service")
        deployment = next(item for item in backend if item["kind"] == "Deployment")
        selector = policy["spec"]["podSelector"]["matchLabels"]
        assert selector == {**service["spec"]["selector"], "stage": "prod"}
        pod_labels = {**deployment["spec"]["template"]["metadata"]["labels"], "stage": "prod"}
        assert all(pod_labels.get(key) == value for key, value in selector.items())

    def test_dispatcher_children_bound_sync_retries(self) -> None:
        teams = yaml.safe_load(Path(sys.argv[-1]).with_name("teams.yaml").read_text())
        expected = {
            "limit": 10,
            "refresh": True,
            "backoff": {"duration": "10s", "factor": 2, "maxDuration": "3m"},
        }
        for app in [*self.apps, teams]:
            with self.subTest(dispatcher=app["metadata"]["name"]):
                policy = app["spec"]["template"]["spec"]["syncPolicy"]
                rendered = self.render(
                    {"spec": {"templatePatch": yaml.safe_dump({"spec": {"syncPolicy": policy}})}},
                    [{"component": "retry-contract"}],
                )
                assert rendered.returncode == 0, rendered.stderr
                child = yaml.safe_load(rendered.stdout)
                assert child["spec"]["syncPolicy"]["retry"] == expected
                assert child["spec"]["syncPolicy"]["automated"]["selfHeal"] is True

    __test__ = True

    @staticmethod
    def _assert_common_sources(sources: dict[str, Any], profile: str, replicas: int) -> None:
        if "dragonfly-peer" in sources:
            assert sources["dragonfly-peer"][0]["helm"]["valueFiles"] == [
                "$values/src/infra/argocd/components/dragonfly_peer/helm/values.yaml",
                "$values/src/infra/argocd/components/dragonfly_peer/helm/client-image.yaml",
            ]
        cert_manager = sources["cert-manager"][0]["helm"]["valuesObject"]
        assert cert_manager["replicaCount"] == replicas
        for controller in (cert_manager, cert_manager["webhook"], cert_manager["cainjector"]):
            assert controller["podDisruptionBudget"]["enabled"] == (profile == "production")
        vpa = sources["vpa"][0]["helm"]["valuesObject"]
        for controller in ("admissionController", "recommender", "updater"):
            assert vpa[controller]["replicas"] == replicas
            assert vpa[controller]["podDisruptionBudget"]["enabled"] == (profile == "production")
        gateway = sources["envoy-gateway-instance"][0]["helm"]["valuesObject"]
        assert gateway["proxy"]["autoscaling"]["minReplicas"] == replicas
        assert gateway["proxy"]["autoscaling"]["maxReplicas"] == (1 if profile == "minimal" else 8)
        collector = sources["otel-collector"][0]["helm"]["valuesObject"]
        assert {"name": "K8S_CLUSTER_NAME", "value": "test-cluster"} in collector["extraEnvs"]
        bridge = sources["prometheus-api-bridge"][0]["helm"]["valuesObject"]
        assert bridge["clusterName"] == "test-cluster"

    @staticmethod
    def _assert_cell_and_signoz_sources(
        app_name: str, sources: dict[str, Any], profile: str
    ) -> None:
        if app_name == "cell-apps":
            bridge = sources["prometheus-api-bridge"][0]["helm"]["valuesObject"]
            assert (
                bridge["backend"]["signoz"]["url"] == "https://observability.c.corp.local.internal"
            )
            for component_name, idx in (
                ("prometheus-api-bridge", 2),
                ("kube-oidc-proxy", 0),
                ("velero-ui", 2),
            ):
                patches = sources[component_name][idx]["kustomize"]["patches"]
                assert any(
                    patch["target"].get("kind") == "Bundle"
                    and "control-cluster-ca" in patch["patch"]
                    for patch in patches
                )
            tailscale = sources["tailscale-access"][0]["helm"]["valuesObject"]
            assert tailscale["local"]["recordName"] == "headscale-preauth-test-cluster"
        trivy_server = sources["trivy-server"][0]["helm"]["valuesObject"]
        assert trivy_server["replicaCount"] == 1
        if "signoz-operator" in sources and profile == "minimal":
            signoz = sources["signoz-operator"][0]["helm"]["valuesObject"]
            assert signoz["controller"]["args"] == [
                "--watch-namespaces=signoz",
                "--leader-elect=false",
            ]
        if "homer" in sources:
            homer_patches = sources["homer"][0]["kustomize"]["patches"]
            expected_clusters = "test-cluster cell-eaws-lh1"
            assert any(
                patch["target"].get("kind") == "Deployment"
                and f"value: {expected_clusters}" in patch["patch"]
                for patch in homer_patches
            )

    def test_selected_profile_controls_generated_sources(self) -> None:
        for app in self.apps:
            generators = app["spec"]["generators"][0]["matrix"]["generators"]
            cluster_inputs = generators[0]["matrix"]["generators"]
            registry_source = next(g["git"] for g in cluster_inputs if "git" in g)
            assert registry_source["files"] == [{"path": "src/infra/argocd/apps/profiles.yaml"}]
            assert registry_source["pathParamPrefix"]
            components = generators[1]["list"]["elements"]
            for profile, replicas in (("minimal", 1), ("production", 2)):
                with self.subTest(app=app["metadata"]["name"], profile=profile):
                    parameters = [self.parameters(c, profile) for c in components]
                    rendered = self.render(app, parameters)
                    assert rendered.returncode == 0, rendered.stderr
                    patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
                    sources = {
                        patch["metadata"]["name"]: patch["spec"]["sources"] for patch in patches
                    }
                    self._assert_common_sources(sources, profile, replicas)
                    self._assert_cell_and_signoz_sources(app["metadata"]["name"], sources, profile)

    def test_clickhouse_profiles_remove_vpas_for_absent_replicas(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(c for c in components if c["component"] == "clickhouse")
        for profile, shards, replicas, keepers in (
            ("minimal", 1, 1, 1),
            ("production", 2, 2, 3),
        ):
            with self.subTest(profile=profile):
                rendered = self.render(app, [self.parameters(component, profile)])
                assert rendered.returncode == 0, rendered.stderr
                application = yaml.safe_load(rendered.stdout)
                source = next(
                    source
                    for source in application["spec"]["sources"]
                    if source.get("path") == component["path"]
                )
                with tempfile.TemporaryDirectory() as directory:
                    overlay = Path(directory)
                    (overlay / "base.yaml").write_text(self.clickhouse_base)
                    (overlay / "kustomization.yaml").write_text(
                        yaml.safe_dump({
                            "apiVersion": "kustomize.config.k8s.io/v1beta1",
                            "kind": "Kustomization",
                            "resources": ["base.yaml"],
                            "patches": source.get("kustomize", {}).get("patches", []),
                        })
                    )
                    manifests = subprocess.run(
                        [self.kustomize, "build", str(overlay)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                assert manifests.returncode == 0, manifests.stderr
                resources = list(yaml.safe_load_all(manifests.stdout))
                installations = {
                    resource["kind"]: resource
                    for resource in resources
                    if resource["kind"]
                    in {"ClickHouseInstallation", "ClickHouseKeeperInstallation"}
                }
                clickhouse = installations["ClickHouseInstallation"]
                keeper = installations["ClickHouseKeeperInstallation"]
                assert clickhouse["spec"]["configuration"]["clusters"][0]["layout"] == {
                    "shardsCount": shards,
                    "replicasCount": replicas,
                }
                keeper_cluster = keeper["spec"]["configuration"]["clusters"][0]
                assert keeper_cluster["layout"]["replicasCount"] == keepers
                assert keeper_cluster["pdbManaged"] == str(keepers > 1).lower()
                for installation in installations.values():
                    assert (
                        installation["spec"]["defaults"]["storageManagement"]["reclaimPolicy"]
                        == "Retain"
                    )
                targets = [
                    resource["spec"]["targetRef"]["name"]
                    for resource in resources
                    if resource["kind"] == "VerticalPodAutoscaler"
                ]
                expected_targets = {
                    f"chi-signoz-clickhouse-cluster-{shard}-{replica}"
                    for shard in range(shards)
                    for replica in range(replicas)
                } | {f"chk-signoz-keeper-keeper-0-{replica}" for replica in range(keepers)}
                assert set(targets) == expected_targets
                assert len(targets) == len(expected_targets)

    def test_local_clickhouse_storage_stats_provider_configures_inventory_manifest_sources(
        self,
    ) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(c for c in components if c["component"] == "clickhouse")
        for profile in ("minimal", "production"):
            with self.subTest(profile=profile):
                rendered = self.render(app, [self.parameters(component, profile)])
                assert rendered.returncode == 0, rendered.stderr
                application = yaml.safe_load(rendered.stdout)
                source = next(
                    source
                    for source in application["spec"]["sources"]
                    if source.get("path") == component["path"]
                )
                patches = source.get("kustomize", {}).get("patches", [])
                stats_patches = [
                    patch
                    for patch in patches
                    if patch["target"].get("name") == "signoz-clickhouse-storage-stats-provider"
                ]
                assert len(stats_patches) == 1
                ops = {op["path"]: op["value"] for op in yaml.safe_load(stats_patches[0]["patch"])}
                assert ops["/data/INVENTORY_ENABLED"] == "true"
                assert ops["/data/INVENTORY_MANIFEST_SOURCES"] == (
                    "cell-eaws-lh1|floci||home||home|http://172.19.0.2:4566/cloud-cell-eaws-lh1-meta/inventory/cell-eaws-lh1/home/{date}/manifest.json\n"
                    "cell-eaws-lh1|floci||scratch||scratch|http://172.19.0.2:4566/cloud-cell-eaws-lh1-meta/inventory/cell-eaws-lh1/scratch/{date}/manifest.json\n"
                    "cell-eaws-lh1|floci||meta||meta|http://172.19.0.2:4566/cloud-cell-eaws-lh1-meta/inventory/cell-eaws-lh1/meta/{date}/manifest.json\n"
                    "cell-eaws-lh1|floci||backups||backups|http://172.19.0.2:4566/cloud-cell-eaws-lh1-meta/inventory/cell-eaws-lh1/backups/{date}/manifest.json\n"
                    "cell-eaws-lh1|floci||archive||archive|http://172.19.0.2:4566/cloud-cell-eaws-lh1-meta/inventory/cell-eaws-lh1/archive/{date}/manifest.json"
                )

        params = self.parameters(component, "minimal")
        params["metadata"]["annotations"]["registered-cells"] = "cell-custom"
        params["metadata"]["annotations"]["s3-endpoint"] = "http://10.0.0.1:4566"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        application = yaml.safe_load(rendered.stdout)
        source = next(
            s for s in application["spec"]["sources"] if s.get("path") == component["path"]
        )
        patches = source.get("kustomize", {}).get("patches", [])
        stats_patches = [
            p
            for p in patches
            if p["target"].get("name") == "signoz-clickhouse-storage-stats-provider"
        ]
        assert len(stats_patches) == 1
        ops = {op["path"]: op["value"] for op in yaml.safe_load(stats_patches[0]["patch"])}
        assert (
            "cell-custom|floci||home||home|http://10.0.0.1:4566/cloud-cell-custom-meta/inventory/cell-custom/home/{date}/manifest.json"
            in ops["/data/INVENTORY_MANIFEST_SOURCES"]
        )

        for provider in ("aws", "gcp"):
            params = self.parameters(component, "minimal")
            params["metadata"]["labels"]["provider"] = provider
            rendered = self.render(app, [params])
            assert rendered.returncode == 0, rendered.stderr
            application = yaml.safe_load(rendered.stdout)
            source = next(
                s for s in application["spec"]["sources"] if s.get("path") == component["path"]
            )
            patches = source.get("kustomize", {}).get("patches", [])
            assert not any(
                p["target"].get("name") == "signoz-clickhouse-storage-stats-provider"
                for p in patches
            )

    def test_clickhouse_backup_provider_patches_backup_cell(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(c for c in components if c["component"] == "clickhouse")
        for profile in ("minimal", "production"):
            with self.subTest(profile=profile):
                rendered = self.render(app, [self.parameters(component, profile)])
                assert rendered.returncode == 0, rendered.stderr
                application = yaml.safe_load(rendered.stdout)
                source = next(
                    s for s in application["spec"]["sources"] if s.get("path") == component["path"]
                )
                patches = source.get("kustomize", {}).get("patches", [])
                cell_patches = [
                    p
                    for p in patches
                    if p["target"].get("name") == "signoz-clickhouse-backup-provider"
                    and "/data/BACKUP_CELL" in p["patch"]
                ]
                assert len(cell_patches) == 1
                ops = {op["path"]: op["value"] for op in yaml.safe_load(cell_patches[0]["patch"])}
                assert ops["/data/BACKUP_CELL"] == "test-cluster"

    def test_local_storage_inventory_producer_is_dispatched_for_floci_cells(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        components = [
            component
            for generator in app["spec"]["generators"]
            for component in generator["matrix"]["generators"][1]["list"]["elements"]
        ]
        component = next(
            item for item in components if item["component"] == "local-storage-inventory-producer"
        )
        assert (
            component["path"]
            == "src/infra/argocd/components/local_storage_inventory_producer/kustomize"
        )
        assert component["namespace"] == "s3-system"
        assert component["wave"] == "30"

        for profile in ("minimal", "production"):
            with self.subTest(profile=profile):
                params = self.parameters(component, profile)
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                child = yaml.safe_load(rendered.stdout)
                sources = child["spec"]["sources"]
                assert len(sources) == 1
                assert (
                    sources[0]["path"]
                    == "src/infra/argocd/components/local_storage_inventory_producer/kustomize"
                )
                assert not any(
                    s.get("path") == "src/infra/argocd/components/default_vpa/helm" for s in sources
                )

    def test_karpenter_aws_dispatched_with_overlay_path_and_helm_values(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        karpenter_elements = [
            component
            for generator in app["spec"]["generators"]
            for component in generator["matrix"]["generators"][1]["list"]["elements"]
            if component["component"] == "karpenter-aws"
        ]
        assert karpenter_elements
        for component in karpenter_elements:
            assert (
                component["path"]
                == "src/infra/argocd/components/karpenter_aws/kustomize/overlays/cells"
            )

        component = karpenter_elements[0]
        for provider in ("aws", "floci"):
            with self.subTest(provider=provider):
                params = self.parameters(component, "production")
                params["metadata"]["labels"]["provider"] = provider
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                child = yaml.safe_load(rendered.stdout)
                sources = child["spec"]["sources"]
                helm_source = sources[0]
                assert helm_source["chart"] == "karpenter"
                assert (
                    "$values/src/infra/argocd/components/karpenter_aws/helm/values.yaml"
                    in helm_source["helm"]["valueFiles"]
                )
                assert (
                    helm_source["helm"]["valuesObject"]["settings"]["clusterName"] == "test-cluster"
                )
                kustomize_source = next(
                    s
                    for s in sources
                    if s.get("path")
                    == "src/infra/argocd/components/karpenter_aws/kustomize/overlays/cells"
                )
                assert kustomize_source is not None
                patches = kustomize_source["kustomize"]["patches"]
                targets = [p["target"]["name"] for p in patches]
                assert "dynamic-cpu" in targets
                assert "dynamic-gpu" in targets

    def test_registry_edit_changes_rendered_replicas(self) -> None:
        app = self.apps[0]
        component = {
            "component": "cert-manager",
            "chart": "cert-manager",
            "path": "helm",
            "chartRepo": "https://example.invalid",
            "chartVersion": "1",
        }
        parameters = self.parameters(component, "minimal")
        parameters["profiles"]["minimal"]["components"]["cert-manager"]["helmValues"][
            "replicaCount"
        ] = EXPECTED_CUSTOM_REPLICAS
        rendered = self.render(app, [parameters])
        assert rendered.returncode == 0, rendered.stderr
        patch = yaml.safe_load(rendered.stdout)
        assert (
            patch["spec"]["sources"][0]["helm"]["valuesObject"]["replicaCount"]
            == EXPECTED_CUSTOM_REPLICAS
        )

    def test_dragonfly_local_image_is_scoped_to_floci(self) -> None:
        image = {
            "digest": self.dragonfly_inventory["imageDigest"],
            "registry": "localhost:15100",
            "repository": "000000000000/us-east-1/dragonfly-client",
            "tag": self.dragonfly_inventory["tag"],
        }
        assert self.dragonfly_client_image == {
            "client": {"image": image},
            "seedClient": {"image": image},
        }
        infrastructure_image = next(
            entry
            for entry in self.infrastructure_inventory["images"]
            if entry["source"] == "src/third_party/dragonflyoss/client"
        )
        assert infrastructure_image["tag"] == image["tag"]
        assert infrastructure_image["imageDigest"] == image["digest"]

        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "dragonfly-peer")
        base_values = "$values/src/infra/argocd/components/dragonfly_peer/helm/values.yaml"
        client_image = "$values/src/infra/argocd/components/dragonfly_peer/helm/client-image.yaml"
        for provider, value_files in (
            ("floci", [base_values, client_image]),
            ("aws", [base_values]),
        ):
            with self.subTest(provider=provider):
                parameters = self.parameters(component, "minimal")
                parameters["metadata"]["labels"]["provider"] = provider

                rendered = self.render(app, [parameters])

                assert rendered.returncode == 0, rendered.stderr
                patch = yaml.safe_load(rendered.stdout)
                helm = patch["spec"]["sources"][0]["helm"]
                assert helm["valueFiles"] == value_files
                values = helm["valuesObject"]
                assert "client" not in values
                assert "image" not in values["seedClient"]

    @staticmethod
    def _assert_repository_charts(patches: list[dict[str, Any]]) -> None:
        repository_charts = [
            source
            for patch in patches
            for source in patch["spec"]["sources"]
            if source.get("path", "").endswith("/helm")
        ]
        for source in repository_charts:
            assert "helm" in source, source["path"]
            assert "kustomize" not in source, source["path"]

    def test_profile_patches_do_not_force_repository_charts_to_kustomize(self) -> None:
        for app in self.apps:
            generators = app["spec"]["generators"][0]["matrix"]["generators"]
            components = generators[1]["list"]["elements"]
            for profile in ("minimal", "production"):
                with self.subTest(app=app["metadata"]["name"], profile=profile):
                    rendered = self.render(app, [self.parameters(c, profile) for c in components])
                    assert rendered.returncode == 0, rendered.stderr
                    patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
                    self._assert_repository_charts(patches)

    def _verify_profile_matrix(
        self,
        app: dict[str, Any],
        components: list[dict[str, Any]],
        labels: dict[str, Any],
        profile: str,
    ) -> None:
        with self.subTest(app=app["metadata"]["name"], labels=labels, profile=profile):
            parameters = [self.parameters(c, profile) for c in components]
            for case in parameters:
                case["metadata"]["labels"].update(labels)
            rendered = self.render(app, parameters)
            assert rendered.returncode == 0, rendered.stderr
            patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
            assert {patch["metadata"]["name"] for patch in patches} == {
                component["component"] for component in components
            }

    def test_provider_specific_generators_supply_profiles_to_every_component(self) -> None:
        for app in self.apps:
            for generator in app["spec"]["generators"]:
                inputs = generator["matrix"]["generators"]
                cluster_inputs = inputs[0]["matrix"]["generators"]
                registry_source = next(g["git"] for g in cluster_inputs if "git" in g)
                assert registry_source["files"] == [{"path": "src/infra/argocd/apps/profiles.yaml"}]
                assert registry_source["pathParamPrefix"]
                cluster_source = next(g["clusters"] for g in cluster_inputs if "clusters" in g)
                labels = cluster_source["selector"].get("matchLabels", {})
                components = inputs[1]["list"]["elements"]
                for profile in ("minimal", "production"):
                    self._verify_profile_matrix(app, components, labels, profile)

    def test_unknown_profile_is_rejected(self) -> None:
        for app in self.apps:
            with self.subTest(app=app["metadata"]["name"]):
                rendered = self.render(app, [self.parameters({"component": "vpa"}, "unknown")])
                assert rendered.returncode != 0

    def test_explicit_profile_overrides_provider_default(self) -> None:
        app = self.apps[0]
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(
            item for item in components if item["component"] == "envoy-gateway-instance"
        )
        for provider, profile, acme_enabled, expected in (
            ("floci", "production", False, 2),
            ("aws", "minimal", False, 1),
            ("aws", "minimal", True, 1),
            ("floci", "", False, 1),
            ("aws", "", False, 2),
        ):
            with self.subTest(provider=provider, profile=profile, acme_enabled=acme_enabled):
                parameters = self.parameters(component, profile)
                parameters["metadata"]["labels"]["provider"] = provider
                if acme_enabled:
                    parameters["metadata"]["labels"]["acme-dns01"] = "enabled"
                rendered = self.render(app, [parameters])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.load(rendered.stdout, Loader=UniqueKeyLoader)[  # ruff: ignore[unsafe-yaml-load]
                    "spec"
                ]["sources"][0]["helm"]["valuesObject"]
                assert values["proxy"]["autoscaling"]["minReplicas"] == expected
                if acme_enabled:
                    assert values["certificate"]["issuerRef"]["name"] == "public-acme"
                else:
                    assert values["certificate"]["enabled"]
                    assert values["certificate"]["issuerRef"]["name"] == "cluster-local-ca"

    def test_buildbuddy_profiles_scale_resources_and_replicas(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        proxy_component = {
            "component": "buildbuddy-enterprise-cache-proxy",
            "chart": "buildbuddy-enterprise-cache-proxy",
            "path": "helm",
            "chartRepo": "https://helm.buildbuddy.io",
            "helmValues": "src/infra/argocd/components/buildbuddy_enterprise_cache_proxy/helm/values.yaml",
        }
        executor_component = {
            "component": "buildbuddy-executor",
            "chart": "buildbuddy-executor",
            "path": "helm",
            "chartRepo": "https://helm.buildbuddy.io",
            "helmValues": "src/infra/argocd/components/buildbuddy_executor/helm/values.yaml",
        }
        for profile, expected_replicas, expected_proxy_cpu, expected_exec_cpu in (
            ("minimal", 1, "100m", "100m"),
            ("production", 3, "4", "4"),
        ):
            with self.subTest(component="proxy", profile=profile):
                params = self.parameters(proxy_component, profile)
                params["metadata"]["labels"]["buildbuddy.io/mode"] = "cloud"
                params["metadata"]["labels"]["buildbuddy.io/enterprise-proxy"] = "enabled"
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.load(rendered.stdout, Loader=UniqueKeyLoader)["spec"]["sources"][0][  # ruff: ignore[unsafe-yaml-load]
                    "helm"
                ]["valuesObject"]
                assert values["replicas"] == expected_replicas
                assert values["resources"]["requests"]["cpu"] == expected_proxy_cpu
                assert values["podDisruptionBudget"]["enabled"] == (profile == "production")

            with self.subTest(component="executor-cpu", profile=profile):
                params = self.parameters(executor_component, profile)
                params["metadata"]["labels"]["buildbuddy.io/mode"] = "cloud"
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.load(rendered.stdout, Loader=UniqueKeyLoader)["spec"]["sources"][0][  # ruff: ignore[unsafe-yaml-load]
                    "helm"
                ]["valuesObject"]
                assert values["resources"]["requests"]["cpu"] == expected_exec_cpu
                assert values["poolName"] == "cpu-pool"

            with self.subTest(component="executor-gpu", profile=profile):
                params = self.parameters(executor_component, profile)
                params["metadata"]["labels"]["buildbuddy.io/mode"] = "cloud"
                params["metadata"]["labels"]["buildbuddy.io/executors"] = "gpu"
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.load(rendered.stdout, Loader=UniqueKeyLoader)["spec"]["sources"][0][  # ruff: ignore[unsafe-yaml-load]
                    "helm"
                ]["valuesObject"]
                gpu_req_cpu = "100m" if profile == "minimal" else "8"
                assert values["resources"]["requests"]["cpu"] == gpu_req_cpu
                assert values["poolName"] == "gpu"
                assert values["resources"]["requests"]["nvidia.com/gpu"] == "1"
                assert values["config"]["executor"]["oci"]["cdi_devices"] == ["nvidia.com/gpu=all"]

            with self.subTest(component="executor-gpu-component", profile=profile):
                gpu_component = {
                    "component": "buildbuddy-executor-gpu",
                    "chart": "buildbuddy-executor",
                    "path": "helm",
                    "chartRepo": "https://helm.buildbuddy.io",
                    "helmValues": "src/infra/argocd/components/buildbuddy_executor/helm/values.yaml",
                }
                params = self.parameters(gpu_component, profile)
                params["metadata"]["labels"]["buildbuddy.io/mode"] = "cloud"
                params["metadata"]["labels"]["buildbuddy.io/executors"] = "all"
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.load(rendered.stdout, Loader=UniqueKeyLoader)["spec"]["sources"][0][  # ruff: ignore[unsafe-yaml-load]
                    "helm"
                ]["valuesObject"]
                gpu_req_cpu = "100m" if profile == "minimal" else "8"
                assert values["resources"]["requests"]["cpu"] == gpu_req_cpu
                assert values["poolName"] == "gpu"
                assert values["resources"]["requests"]["nvidia.com/gpu"] == "1"
                assert values["config"]["executor"]["oci"]["cdi_devices"] == ["nvidia.com/gpu=all"]

    def test_local_retention_overrides(self) -> None:
        minimal = self.registry["profiles"]["minimal"]["components"]
        production = self.registry["profiles"]["production"]["components"]

        assert (
            minimal["velero"]["helmValues"]["schedules"]["cluster-daily"]["template"]["ttl"]
            == "48h0m0s"
        )
        assert "velero" not in production

        signoz_minimal_patches = minimal["signoz"]["patches"]
        signoz_patch = next(
            p for p in signoz_minimal_patches if p["target"]["name"] == "signoz-log-retention"
        )
        assert signoz_patch["target"]["kind"] == "Job"
        assert "value: '3'" in signoz_patch["patch"]

        clickhouse_minimal_patches = minimal["clickhouse"]["patches"]
        ch_patch = next(
            p
            for p in clickhouse_minimal_patches
            if p["target"]["name"] == "signoz-clickhouse-backup-common"
        )
        assert ch_patch["target"]["kind"] == "ConfigMap"
        assert "value: '1'" in ch_patch["patch"]

    @staticmethod
    def _assert_sources_repo_url(patches: list[dict[str, Any]], expected_url: str) -> None:
        for patch in patches:
            for source in patch["spec"]["sources"]:
                if "path" in source or source.get("ref") == "values":
                    assert source["repoURL"] == expected_url

    def test_git_sources_resolve_custom_repository_url_from_annotations(self) -> None:
        custom_repo = "https://github.com/custom-org/platform-gitops.git"
        for app in self.apps:
            components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
            params = [self.parameters(c, "minimal") for c in components]
            for p in params:
                p["metadata"]["annotations"]["git-repo-url"] = custom_repo
            rendered = self.render(app, params)
            assert rendered.returncode == 0, rendered.stderr
            patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
            self._assert_sources_repo_url(patches, custom_repo)

    def test_repo_url(self) -> None:
        for app in self.apps:
            components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
            params = [self.parameters(c, "minimal") for c in components]
            expected_repo = params[0]["metadata"]["annotations"]["git-repo-url"]
            rendered = self.render(app, params)
            assert rendered.returncode == 0, rendered.stderr
            patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
            self._assert_sources_repo_url(patches, expected_repo)

    def test_coder_patch_merges_environment_by_name(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "coder")
        parameters = self.parameters(component, "minimal")
        expected_repo = "git://192.0.2.21:9418/repo.git"
        parameters["metadata"]["annotations"]["git-transport-url"] = expected_repo

        rendered = self.render(app, [parameters])

        assert rendered.returncode == 0, rendered.stderr
        application = yaml.safe_load(rendered.stdout)
        source = next(
            source
            for source in application["spec"]["sources"]
            if source.get("path") == component["path"]
        )
        patch = next(
            item
            for item in source["kustomize"]["patches"]
            if item["target"].get("name") == "coder-template-reconciler"
        )
        assert not patch["patch"].lstrip().startswith("- op:")
        job = yaml.safe_load(patch["patch"])
        pod_spec = job["spec"]["template"]["spec"]
        assert pod_spec["initContainers"] == [
            {"name": "source", "env": [{"name": "SOURCE_REPOSITORY", "value": expected_repo}]},
        ]
        reconcile = pod_spec["containers"][0]
        assert reconcile["name"] == "reconcile"
        reconcile_env_dict = {entry["name"]: entry["value"] for entry in reconcile["env"]}
        assert reconcile_env_dict["CODER_TEMPLATE_ACCESS_ALIAS_DOMAIN"] == "c.corp.local.internal"
        assert reconcile_env_dict["CODER_TEMPLATE_DEPLOYMENT_DOMAIN"] == "corp.local.internal"
        assert (
            reconcile_env_dict["CODER_TEMPLATE_HEADSCALE_URL"]
            == "https://headscale.test-cluster.c.corp.local.internal"
        )
        assert reconcile_env_dict["CODER_TEMPLATE_REPOSITORY_URL"] == expected_repo
        assert reconcile_env_dict["CODER_TEMPLATE_STORAGE_CLASS"] == "workspace-expandable"
        assert reconcile_env_dict["CODER_TEMPLATE_ARCH"] == "arm64"
        assert reconcile_env_dict["CODER_TEMPLATE_CELL"] == "cell-eaws-lh1"
        assert reconcile_env_dict["CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY"] == json.dumps(
            {"cell-eaws-lh1": "0123456789ab"}, separators=(",", ":")
        )
        assert reconcile_env_dict["CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY"] == json.dumps(
            {"cell-eaws-lh1": "eaws-lh1"}, separators=(",", ":")
        )

        parameters["metadata"]["labels"]["provider"] = "aws"
        parameters["metadata"]["annotations"]["ecr-registry"] = (
            "111122223333.dkr.ecr.us-west-2.amazonaws.com"
        )
        cloud_rendered = self.render(app, [parameters])
        assert cloud_rendered.returncode == 0, cloud_rendered.stderr
        cloud_application = yaml.safe_load(cloud_rendered.stdout)
        cloud_source = next(
            source
            for source in cloud_application["spec"]["sources"]
            if source.get("path") == component["path"]
        )
        cloud_patch = next(
            item
            for item in cloud_source["kustomize"]["patches"]
            if item["target"].get("name") == "coder-template-reconciler"
        )
        cloud_job = yaml.safe_load(cloud_patch["patch"])
        cloud_env = cloud_job["spec"]["template"]["spec"]["containers"][0]["env"]
        cloud_env_dict = {entry["name"]: entry["value"] for entry in cloud_env}
        assert cloud_env_dict["CODER_TEMPLATE_STORAGE_CLASS"] == "general-expandable"
        assert cloud_env_dict["CODER_TEMPLATE_HEADSCALE_URL"] == ""
        assert cloud_env_dict["CODER_TEMPLATE_ARCH"] == "amd64"
        assert cloud_env_dict["CODER_TEMPLATE_CELL"] == "cell-eaws-lh1"
        assert cloud_env_dict["CODER_TEMPLATE_WORKLOAD_ORIGIN_PROVIDER"] == "aws"
        assert cloud_env_dict["CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY"] == json.dumps(
            {"cell-eaws-lh1": "0123456789ab"}, separators=(",", ":")
        )
        assert cloud_env_dict["CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY"] == json.dumps(
            {"cell-eaws-lh1": "eaws-lh1"}, separators=(",", ":")
        )
        cloud_init = cloud_job["spec"]["template"]["spec"]["initContainers"]
        assert len(cloud_init) == 3
        assert [c["name"] for c in cloud_init] == [
            "source",
            "ecr-login",
            "resolve-workspace-image",
        ]
        assert cloud_init[0] == {
            "name": "source",
            "env": [{"name": "SOURCE_REPOSITORY", "value": expected_repo}],
        }
        assert cloud_init[1]["name"] == "ecr-login"
        assert (
            cloud_init[1]["image"]
            == "docker.io/amazon/aws-cli:2.37.5@sha256:fb7ccfc7b4e3a05017e6c9ded4ba959b99af622130e211fb976c68c2f4d3f224"
        )
        assert cloud_init[2]["name"] == "resolve-workspace-image"
        resolve_env = {e["name"]: e["value"] for e in cloud_init[2]["env"]}
        assert (
            resolve_env["WORKSPACE_IMAGE_SOURCE"]
            == "111122223333.dkr.ecr.us-west-2.amazonaws.com/src/infra/definitions/workspaces/templates/dev:dev-v1"
        )
        assert (
            resolve_env["WORKSPACE_IMAGE_REPOSITORY"]
            == "111122223333.dkr.ecr.us-west-2.amazonaws.com/src/infra/definitions/workspaces/templates/dev"
        )
        assert resolve_env["WORKSPACE_IMAGE_SOURCE_INSECURE"] == "false"
        assert resolve_env["DOCKER_CONFIG"] == "/state/.docker"
        assert cloud_env_dict["CODER_SESSION_TOKEN_FILE"] == "/var/run/coder/automation-token/token"
        assert cloud_env_dict["PRESERVE_CODER_SESSION_TOKEN"] == "true"

        cloud_healer_patch = next(
            item
            for item in cloud_source["kustomize"]["patches"]
            if item["target"].get("name") == "coder-workspace-healer"
        )
        assert (
            "- op: replace\n  path: /spec/template/spec/initContainers"
            in cloud_healer_patch["patch"]
        )
        assert "/var/run/coder/automation-token/token" in cloud_healer_patch["patch"]

        # The coder namespace reads the token record through runtime-secrets; only the
        # application-identity record is pointed at the cloud store.
        assert not any(
            item["target"].get("name") == "coder-automation-token"
            for item in cloud_source["kustomize"]["patches"]
        )

    def test_coder_automation_token_record_uses_cloud_store_only_on_cloud(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "external-secrets")
        for provider in ("aws", "floci"):
            with self.subTest(provider=provider):
                params = self.parameters(component, "minimal")
                params["metadata"]["labels"]["provider"] = provider
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                application = yaml.safe_load(rendered.stdout)
                patches = [
                    patch
                    for source in application["spec"]["sources"]
                    for patch in source.get("kustomize", {}).get("patches", [])
                    if patch["target"].get("name") == "coder-automation-token"
                ]
                if provider == "floci":
                    assert patches == []
                else:
                    assert len(patches) == 1
                    operations = {
                        op["path"]: op["value"] for op in yaml.safe_load(patches[0]["patch"])
                    }
                    assert operations["/spec/secretStoreRef/name"] == "aws-secrets-manager"
                    assert (
                        operations["/spec/data/0/remoteRef/key"]
                        == "test-cluster-coder-automation-token"
                    )

    def test_coder_template_reconciler_zero_registered_cells(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "coder")
        parameters = self.parameters(component, "minimal")
        parameters["metadata"]["labels"]["provider"] = "aws"
        parameters["metadata"]["annotations"]["registered-cells"] = ""
        parameters["metadata"]["annotations"]["ecr-registry"] = (
            "111122223333.dkr.ecr.us-west-2.amazonaws.com"
        )
        rendered = self.render(app, [parameters])
        assert rendered.returncode == 0, rendered.stderr
        application = yaml.safe_load(rendered.stdout)
        source = next(
            s for s in application["spec"]["sources"] if s.get("path") == component["path"]
        )
        patch = next(
            item
            for item in source["kustomize"]["patches"]
            if item["target"].get("name") == "coder-template-reconciler"
        )
        job = yaml.safe_load(patch["patch"])
        env = {
            entry["name"]: entry["value"]
            for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["CODER_TEMPLATE_CELL"] == ""
        assert env["CODER_TEMPLATE_WORKSPACE_INCARNATION_INVENTORY"] == "{}"
        assert env["CODER_TEMPLATE_WORKSPACE_PLACEMENT_INVENTORY"] == "{}"
        assert env["CODER_TEMPLATE_WORKSPACE_VIRTUAL_NAME_INVENTORY"] == "{}"

    @staticmethod
    def _assert_component_flags(
        components: list[dict[str, Any]], allowed: dict[str, set[str]]
    ) -> None:
        for flag, allowlist in allowed.items():
            for comp in components:
                if comp.get(flag):
                    assert comp["component"] in allowlist, (
                        f"{flag} is forbidden on {comp['component']}, only permitted on {allowlist}"
                    )

    def test_generator_inject_flags_are_allowlisted(self) -> None:
        allowed = {
            "injectClusterDomain": {"coredns"},
            "injectClusterProvider": {"routing-registry", "routing-registry-public"},
            "injectClusterName": {"otel-collector", "prometheus-api-bridge", "signoz"},
        }
        for app in self.apps:
            generators = app["spec"]["generators"][0]["matrix"]["generators"]
            components = generators[1]["list"]["elements"]
            self._assert_component_flags(components, allowed)

    def test_rendered_values_object_has_no_duplicate_named_items(self) -> None:
        for app in self.apps:
            generators = app["spec"]["generators"][0]["matrix"]["generators"]
            components = generators[1]["list"]["elements"]
            for profile in ("minimal", "production"):
                rendered = self.render(app, [self.parameters(c, profile) for c in components])
                assert rendered.returncode == 0, rendered.stderr
                patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
                _verify_patches_have_unique_names(patches)


class ProfilesContractTest(_ProfilesBaseTest):
    __test__ = True

    def test_signoz_operator_profile_configures_args_not_env(self) -> None:
        minimal = self.registry["profiles"]["minimal"]["components"]
        if "signoz-operator" in minimal:
            helm_values = minimal["signoz-operator"].get("helmValues", {})
            controller = helm_values.get("controller", {})
            assert "env" not in controller, (
                "signoz-operator must not configure controller.env; upstream hardcodes "
                "SIGNOZ_OPERATOR_LEADER_ELECT and duplicate env keys trigger Server-Side Apply errors"
            )
            assert "--leader-elect=false" in controller.get("args", [])

    def test_kueue_admission_cell_contract(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        component = {
            "component": "kueue-admission",
            "path": "src/infra/argocd/components/kueue_admission/helm",
            "namespace": "kueue-system",
            "wave": "10",
            "helmValues": "values.yaml",
        }
        params = self.parameters(component, "production")
        params["metadata"]["labels"]["provider"] = "aws"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        patch = yaml.safe_load(rendered.stdout)
        helm = patch["spec"]["sources"][0]["helm"]
        assert helm["valueFiles"] == ["values.yaml"]
        values = helm["valuesObject"]
        assert values["role"] == "cell"
        assert values["clusterName"] == "test-cluster"
        assert values["provider"] == "aws"
        assert values["topologies"][0]["name"] == "cell-topology"
        assert [flavor["name"] for flavor in values["flavors"]] == ["on-demand", "spot"]

    def test_floci_certificate_covers_its_webhook_and_registry_addresses(self) -> None:
        inputs = [Path(arg) for arg in " ".join(sys.argv[1:-3]).split()]
        rendered = next(
            path
            for path in inputs
            if path.parent.name == "local_runtime_tls" and path.name == "helm_render.yaml"
        )
        certificate = next(
            resource
            for resource in yaml.safe_load_all(rendered.read_text())
            if resource and resource["metadata"]["name"] == "local-floci-tls"
        )
        self.assertEqual(
            set(certificate["spec"]["ipAddresses"]), {"127.0.0.1", "192.0.2.2", "192.0.2.22"}
        )
        self.assertEqual(
            certificate["spec"]["issuerRef"],
            {"name": "cluster-local-ca", "kind": "ClusterIssuer"},
        )

    def test_local_runtime_certificates_use_the_registry_without_tailnet_dependencies(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        matches = []
        for generator in app["spec"]["generators"]:
            nested, entries = generator["matrix"]["generators"]
            for component in entries["list"]["elements"]:
                if component["component"] == "local-runtime-tls":
                    matches.append(component)
                    labels = nested["matrix"]["generators"][0]["clusters"]["selector"][
                        "matchLabels"
                    ]
                    self.assertEqual(labels, {"provider": "floci", "role": "ctrl"})
        self.assertEqual(len(matches), 1)
        component = matches[0]
        self.assertEqual(component["wave"], "15")
        rendered = self.render(app, [self.parameters(component, "minimal")])
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        sources = yaml.safe_load(rendered.stdout)["spec"]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["helm"]["valueFiles"], ["values.yaml"])
        self.assertEqual(
            sources[0]["helm"]["valuesObject"]["accessAliasDomain"], "c.corp.local.internal"
        )

    def test_local_control_private_access_preserves_cell_destination(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(c for c in components if c["component"] == "tailscale-access")
        params = self.parameters(copy.deepcopy(component), "minimal")
        params["metadata"]["annotations"]["control-gateway-ipv4"] = "192.0.2.10"
        params["helmValuesObject"]["egress"]["gatewayIPv4"] = "192.0.2.20"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        values = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"]["valuesObject"]
        assert values["egress"]["gatewayIPv4"] == "192.0.2.20"

    def test_local_headlamp_trusts_the_imported_cell_ca(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(c for c in components if c["component"] == "headlamp")
        rendered = self.render(app, [self.parameters(component, "minimal")])
        assert rendered.returncode == 0, rendered.stderr
        sources = yaml.safe_load(rendered.stdout)["spec"]["sources"]
        patches = [
            patch
            for source in sources
            for patch in source.get("kustomize", {}).get("patches", [])
            if patch["target"].get("name") == "headlamp-cluster-ca"
        ]
        assert len(patches) == 1
        assert yaml.safe_load(patches[0]["patch"]) == [
            {
                "op": "add",
                "path": "/spec/sources/-",
                "value": {"secret": {"name": "cell-cluster-ca", "key": "ca.crt"}},
            },
        ]

    def test_tailscale_access_cloud_contract(self) -> None:
        for app in self.apps:
            component = {
                "component": "tailscale-access",
                "path": "src/infra/argocd/components/tailscale_access/helm",
                "namespace": "tailscale-system",
                "wave": "40",
                "helmValues": "values.yaml",
            }
            for provider in ("aws", "gcp"):
                with self.subTest(app=app["metadata"]["name"], provider=provider, case="default"):
                    params = self.parameters(component, "production")
                    params["metadata"]["labels"]["provider"] = provider
                    rendered = self.render(app, [params])
                    assert rendered.returncode == 0, rendered.stderr
                    patch = yaml.safe_load(rendered.stdout)
                    values = patch["spec"]["sources"][0]["helm"]["valuesObject"]
                    assert values["mode"] == "cloud-connector"

                with self.subTest(
                    app=app["metadata"]["name"], provider=provider, case="annotations"
                ):
                    params = self.parameters(component, "production")
                    params["metadata"]["labels"]["provider"] = provider
                    params["metadata"]["annotations"]["service-cidr"] = "172.20.0.0/20"
                    params["metadata"]["annotations"]["tailscale-oauth-key"] = "tskey-auth-cloud"
                    params["metadata"]["annotations"]["control-gateway-ipv4"] = "10.0.0.1"
                    rendered = self.render(app, [params])
                    assert rendered.returncode == 0, rendered.stderr
                    patch = yaml.safe_load(rendered.stdout)
                    values = patch["spec"]["sources"][0]["helm"]["valuesObject"]
                    assert values["mode"] == "cloud-connector"
                    assert values["serviceCIDR"] == "172.20.0.0/20"
                    assert values["cloud"]["oauthRemoteKey"] == "tskey-auth-cloud"
                    assert values["egress"]["gatewayIPv4"] == "10.0.0.1"

    def test_control_dns_routes_cell_oidc_hosts_through_private_access(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = [
            component
            for generator in app["spec"]["generators"]
            for component in generator["matrix"]["generators"][1]["list"]["elements"]
        ]
        component = next(
            component for component in components if component["component"] == "coredns"
        )
        assert "dragonfly" in component["helmValuesObject"]["privateAccess"]["recordLabels"]
        for cells, hostname in (
            (
                component["helmValuesObject"]["cellDomains"],
                "kube-oidc-proxy.cell-eaws-lh1.c.corp.local.internal",
            ),
            ({"cell-custom": "c.example.invalid"}, "kube-oidc-proxy.cell-custom.c.example.invalid"),
        ):
            with self.subTest(cells=cells):
                params = self.parameters(copy.deepcopy(component), "minimal")
                params["helmValuesObject"]["cellDomains"] = cells
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"][
                    "valuesObject"
                ]
                assert values["rewrites"] == {
                    hostname: "active-cell-private-access.tailscale-system.svc.cluster.local",
                }

    def test_dragonfly_console_uses_the_gateway_access_domain(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        component = next(
            component
            for generator in app["spec"]["generators"]
            for component in generator["matrix"]["generators"][1]["list"]["elements"]
            if component["component"] == "dragonfly-manager"
        )
        params = self.parameters(component, "minimal")
        params["metadata"]["annotations"]["access-domain"] = "c.example.invalid"
        params["metadata"]["annotations"]["domain"] = "example.invalid"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        application = yaml.safe_load(rendered.stdout)
        source = next(
            source
            for source in application["spec"]["sources"]
            if source.get("path") == component["path"]
        )
        patches = source["kustomize"]["patches"]
        route = next(
            patch for patch in patches if patch["target"].get("name") == "dragonfly-console"
        )
        assert (
            yaml.safe_load(route["patch"])[0]["value"] == "dragonfly.test-cluster.c.example.invalid"
        )
        policy = next(
            patch for patch in patches if patch["target"].get("name") == "dragonfly-console-oidc"
        )
        replacements = {item["path"]: item["value"] for item in yaml.safe_load(policy["patch"])}
        assert replacements["/spec/oidc/provider/issuer"] == "https://dex.example.invalid"
        assert "/spec/oidc/redirectURL" not in replacements
        assert replacements["/spec/jwt/providers/0/issuer"] == "https://dex.example.invalid"

    def test_coredns_cloud_contract(self) -> None:
        for app in self.apps:
            component = {
                "component": "coredns",
                "path": "src/infra/argocd/components/coredns/helm",
                "namespace": "kube-system",
                "wave": "5",
                "helmValues": "values.yaml",
                "injectClusterDomain": True,
                "helmValuesObject": {
                    "control": {
                        "gatewayIPv4": "172.31.0.11",
                        "headscaleIPv4": "172.19.255.23",
                    },
                },
            }
            for provider in ("aws", "gcp"):
                with self.subTest(app=app["metadata"]["name"], provider=provider):
                    params = self.parameters(component, "production")
                    params["metadata"]["labels"]["provider"] = provider
                    params["metadata"]["annotations"]["control-gateway-ipv4"] = "10.0.0.1"
                    params["metadata"]["annotations"]["service-cidr"] = "172.20.0.0/20"
                    rendered = self.render(app, [params])
                    assert rendered.returncode == 0, rendered.stderr
                    patch = yaml.safe_load(rendered.stdout)
                    values = patch["spec"]["sources"][0]["helm"]["valuesObject"]
                    assert values["control"]["headscaleIPv4"] == ""
                    assert values["control"]["gatewayIPv4"] == "10.0.0.1"
                    assert values["serviceCIDR"] == "172.20.0.0/20"

    def test_local_rawfile_driver_uses_seeded_publish_identity_fix(self) -> None:
        image = next(
            entry
            for entry in self.infrastructure_inventory["images"]
            if entry["source"] == "src/third_party/openebs/rawfile-localpv"
        )
        for app in self.apps:
            component = next(
                component
                for generator in app["spec"]["generators"]
                for component in generator["matrix"]["generators"][1]["list"]["elements"]
                if component["component"] == "rawfile-localpv"
            )
            for profile in self.registry["profiles"]:
                with self.subTest(app=app["metadata"]["name"], profile=profile):
                    params = self.parameters(component, profile)
                    params["metadata"]["labels"]["provider"] = "floci"
                    rendered = self.render(app, [params])
                    assert rendered.returncode == 0, rendered.stderr
                    sources = yaml.safe_load(rendered.stdout)["spec"]["sources"]
                    values = next(
                        source["helm"]["valuesObject"]
                        for source in sources
                        if source.get("chart") == "rawfile-localpv"
                    )
                    assert values["image"]["registry"] == "localhost:15100"
                    assert (
                        values["image"]["repository"]
                        == f"000000000000/us-east-1/{image['repository']}"
                    )
                    assert values["image"]["tag"].split("@", 1)[1] == image["imageDigest"]

    def test_dragonfly_bridge_uses_seeded_origin_image_only_for_floci(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "dragonfly-manager")
        image = next(
            entry
            for entry in self.infrastructure_inventory["images"]
            if entry["source"] == "src/infra/tools/dragonfly_sso"
        )
        for provider in ("aws", "floci", "gcp"):
            with self.subTest(provider=provider):
                params = self.parameters(component, "minimal")
                params["metadata"]["labels"]["provider"] = provider
                if provider != "floci":
                    params["metadata"]["annotations"]["ecr-registry"] = (
                        "111122223333.dkr.ecr.us-west-2.amazonaws.com"
                    )
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                patch = yaml.safe_load(rendered.stdout)
                source = next(
                    source
                    for source in patch["spec"]["sources"]
                    if source.get("path") == component["path"]
                )
                patches = source.get("kustomize", {}).get("patches", [])
                bridge_patches = [
                    item for item in patches if item["target"].get("name") == "dragonfly-sso"
                ]
                assert len(bridge_patches) == 1
                operations = yaml.safe_load(bridge_patches[0]["patch"])
                assert operations[0] == {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/env/0/value",
                    "value": "test-cluster",
                }
                if provider == "floci":
                    assert operations[1:] == [
                        {
                            "op": "replace",
                            "path": "/spec/template/spec/containers/0/image",
                            "value": (
                                "localhost:15100/000000000000/us-east-1/"
                                f"{image['repository']}@{image['imageDigest']}"
                            ),
                        }
                    ]
                else:
                    assert operations[1:] == [
                        {
                            "op": "replace",
                            "path": "/spec/template/spec/containers/0/image",
                            "value": (
                                "111122223333.dkr.ecr.us-west-2.amazonaws.com/"
                                "test-cluster/infrastructure:dragonfly-sso@"
                                f"{image['imageDigest']}"
                            ),
                        }
                    ]

    def test_routing_api_key_records_use_cloud_store_only_on_cloud(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "routing-registry")
        for provider in ("aws", "gcp", "floci"):
            with self.subTest(provider=provider):
                params = self.parameters(component, "minimal")
                params["metadata"]["labels"]["provider"] = provider
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"][
                    "valuesObject"
                ]
                assert values["apiKeyRecordSources"] == {}
                assert values["apiKeySecretStore"] == "runtime-secrets"

    def test_snapshot_portal_uses_provider_registry_image(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(
            item for item in components if item["component"] == "coder-snapshot-portal"
        )
        image = next(
            entry
            for entry in self.infrastructure_inventory["images"]
            if entry["source"] == "src/infra/tools/coder_snapshot_portal"
        )
        for provider in ("aws", "floci", "gcp"):
            with self.subTest(provider=provider):
                params = self.parameters(component, "minimal")
                params["metadata"]["labels"]["provider"] = provider
                params["metadata"]["annotations"]["access-domain"] = "c.example.invalid"
                params["metadata"]["annotations"]["domain"] = "example.invalid"
                if provider != "floci":
                    params["metadata"]["annotations"]["ecr-registry"] = (
                        "111122223333.dkr.ecr.us-west-2.amazonaws.com"
                    )
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                patch = yaml.safe_load(rendered.stdout)
                values = patch["spec"]["sources"][0]["helm"]["valuesObject"]
                assert (
                    values["access"]["hostname"] == "coder-snapshots.test-cluster.c.example.invalid"
                )
                if provider == "floci":
                    assert values["image"] == {
                        "repository": f"localhost:15100/000000000000/us-east-1/{image['repository']}",
                        "digest": image["imageDigest"],
                    }
                else:
                    assert values["image"] == {
                        "repository": (
                            "111122223333.dkr.ecr.us-west-2.amazonaws.com/"
                            "test-cluster/infrastructure:coder-snapshot-portal"
                        ),
                        "digest": image["imageDigest"],
                    }

    def test_s3_gateway_uses_registered_floci_endpoint(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(
            component for component in components if component["component"] == "s3-gateway"
        )
        image = next(
            entry
            for entry in self.infrastructure_inventory["images"]
            if entry["source"] == "src/infra/tools/s3_resolver/server"
        )
        for provider in ("aws", "floci", "gcp"):
            with self.subTest(provider=provider):
                params = self.parameters(component, "minimal")
                params["metadata"]["labels"]["provider"] = provider
                params["metadata"]["annotations"]["s3-endpoint"] = "http://192.0.2.10:4566"
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                patch = yaml.safe_load(rendered.stdout)
                values = patch["spec"]["sources"][0]["helm"].get("valuesObject", {})
                patches = patch["spec"]["sources"][1].get("kustomize", {}).get("patches", [])
                resolver_patches = [
                    item for item in patches if item["target"].get("name") == "s3-resolver"
                ]
                if provider == "floci":
                    assert values["flociS3Endpoint"] == "http://192.0.2.10:4566"
                    assert len(resolver_patches) == 1
                    assert yaml.safe_load(resolver_patches[0]["patch"]) == [
                        {
                            "op": "replace",
                            "path": "/spec/template/spec/containers/0/image",
                            "value": (
                                "localhost:15100/000000000000/us-east-1/"
                                f"{image['repository']}@{image['imageDigest']}"
                            ),
                        }
                    ]
                else:
                    assert "flociS3Endpoint" not in values
                    assert not resolver_patches

    def test_s3_gateway_uses_registered_cells(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(
            component for component in components if component["component"] == "s3-gateway"
        )
        for profile, provider, target, registered, writer in (
            ("minimal", "floci", "cell-eaws-lh1", "cell-eaws-lh1", "cell-eaws-lh1"),
            ("production", "aws", "cell-aws-usw2", "cell-aws-usw2", "cell-aws-usw2"),
            (
                "production",
                "gcp",
                "cell-gcp-euw4",
                "cell-aws-usw2,cell-gcp-euw4",
                "cell-aws-usw2",
            ),
        ):
            with self.subTest(profile=profile, target=target):
                params = self.parameters(component, profile)
                params["nameNormalized"] = target
                params["metadata"]["labels"]["provider"] = provider
                params["metadata"]["annotations"]["registered-cells"] = registered
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                values = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"][
                    "valuesObject"
                ]
                assert values["targetCell"] == target
                assert values["registeredCells"] == registered.split(",")
                assert values["writerCell"] == writer
        params["metadata"]["annotations"].pop("registered-cells")
        assert self.render(app, [params]).returncode != 0

    def test_karpenter_aws_cell_contract(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        component = {
            "component": "karpenter-aws",
            "path": "src/infra/argocd/components/karpenter_aws/kustomize",
            "namespace": "karpenter-system",
            "wave": "5",
            "chart": "karpenter",
            "chartRepo": "public.ecr.aws/karpenter",
            "chartVersion": "1.14.0",
        }
        params = self.parameters(component, "production")
        params["metadata"]["labels"]["provider"] = "aws"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        patch = yaml.safe_load(rendered.stdout)
        values = patch["spec"]["sources"][0]["helm"]["valuesObject"]
        assert values["settings"]["clusterName"] == "test-cluster"
        assert values["settings"]["interruptionQueue"] == "test-cluster-karpenter"
        assert values["replicas"] == EXPECTED_KARPENTER_REPLICAS
        patches = patch["spec"]["sources"][2]["kustomize"]["patches"]
        assert any("karpenter.sh~1discovery" in p["patch"] for p in patches)
        assert any("test-cluster" in p["patch"] for p in patches)
        assert not any(p["target"].get("kind") == "NetworkPolicy" for p in patches)

        params_minimal = self.parameters(component, "minimal")
        params_minimal["metadata"]["labels"]["provider"] = "floci"
        params_minimal["metadata"]["annotations"]["s3-endpoint"] = "http://192.0.2.10:14566"
        rendered_minimal = self.render(app, [params_minimal])
        assert rendered_minimal.returncode == 0, rendered_minimal.stderr
        patch_minimal = yaml.safe_load(rendered_minimal.stdout)
        values_minimal = patch_minimal["spec"]["sources"][0]["helm"]["valuesObject"]
        assert values_minimal["settings"]["clusterName"] == "test-cluster"
        assert values_minimal["settings"]["interruptionQueue"] == "test-cluster-karpenter"
        assert values_minimal["replicas"] == 1
        patches_minimal = patch_minimal["spec"]["sources"][2]["kustomize"]["patches"]
        assert any("karpenter.sh~1discovery" in p["patch"] for p in patches_minimal)
        assert any("test-cluster" in p["patch"] for p in patches_minimal)
        network_patches = [p for p in patches_minimal if p["target"].get("kind") == "NetworkPolicy"]
        assert len(network_patches) == 1
        assert network_patches[0]["target"]["name"] == "karpenter-network-isolation"
        assert yaml.safe_load(network_patches[0]["patch"]) == [
            {
                "op": "add",
                "path": "/spec/egress/-",
                "value": {
                    "to": [{"ipBlock": {"cidr": "192.0.2.10/32"}}],
                    "ports": [{"protocol": "TCP", "port": 14566}],
                },
            }
        ]

    def test_karpenter_gcp_cell_contract(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        component = {
            "component": "karpenter-gcp",
            "path": "src/infra/argocd/components/karpenter_gcp/kustomize",
            "namespace": "karpenter-system",
            "wave": "5",
            "chart": "karpenter",
            "chartRepo": "https://cloudpilot-ai.github.io/karpenter-provider-gcp",
            "chartVersion": "0.6.0",
        }
        params = self.parameters(component, "production")
        params["metadata"]["labels"]["provider"] = "gcp"
        params["metadata"]["annotations"]["gcp-project-id"] = "my-gcp-project"
        params["metadata"]["annotations"]["gcp-location"] = "us-central1"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        patch = yaml.safe_load(rendered.stdout)
        values = patch["spec"]["sources"][0]["helm"]["valuesObject"]
        assert values["controller"]["settings"]["clusterName"] == "test-cluster"
        assert values["controller"]["settings"]["projectID"] == "my-gcp-project"
        assert values["controller"]["settings"]["clusterLocation"] == "us-central1"
        assert (
            "my-gcp-project"
            in values["serviceAccount"]["annotations"]["iam.gke.io/gcp-service-account"]
        )
        patches = patch["spec"]["sources"][2]["kustomize"]["patches"]
        assert any(
            "test-cluster-node-pool@my-gcp-project.iam.gserviceaccount.com" in p["patch"]
            for p in patches
        )

    def test_karpenter_aws_ctrl_contract(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        aws_generator = next(
            g
            for g in app["spec"]["generators"]
            if g["matrix"]["generators"][0]["matrix"]["generators"][0]["clusters"]["selector"][
                "matchLabels"
            ].get("provider")
            == "aws"
        )
        elements = aws_generator["matrix"]["generators"][1]["list"]["elements"]
        component = next(e for e in elements if e["component"] == "karpenter-aws")
        assert component["wave"] == "5"
        assert component["namespace"] == "karpenter-system"

        sync_options = app["spec"]["template"]["spec"]["syncPolicy"]["syncOptions"]
        assert "ServerSideApply=true" in sync_options

        params = self.parameters(component, "production")
        params["metadata"]["labels"]["provider"] = "aws"
        params["metadata"]["labels"]["role"] = "ctrl"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        patch = yaml.safe_load(rendered.stdout)
        values = patch["spec"]["sources"][0]["helm"]["valuesObject"]
        assert values["settings"]["clusterName"] == "test-cluster"
        assert values["settings"]["interruptionQueue"] == "test-cluster-karpenter"
        patches = patch["spec"]["sources"][2]["kustomize"]["patches"]
        assert any("karpenter.sh~1discovery" in p["patch"] for p in patches)
        assert any("test-cluster" in p["patch"] for p in patches)
        discovery_patch = next(
            p
            for p in patches
            if p["target"].get("kind") == "EC2NodeClass"
            and p["target"].get("name") == "dynamic-cpu"
        )
        patch_ops = yaml.safe_load(discovery_patch["patch"])
        op_map = {op["path"]: op["value"] for op in patch_ops}
        assert op_map["/spec/subnetSelectorTerms/0/tags/karpenter.sh~1discovery"] == "test-cluster"
        assert (
            op_map["/spec/securityGroupSelectorTerms/0/tags/karpenter.sh~1discovery"]
            == "test-cluster"
        )
        assert op_map["/spec/tags/karpenter.sh~1discovery"] == "test-cluster"
        assert op_map["/spec/tags/cluster"] == "test-cluster"
        assert op_map["/spec/instanceProfile"] == "KarpenterNodeInstanceProfile-test-cluster"

    def test_karpenter_aws_ctrl_overlay_nodepool(self) -> None:
        base_dir = Path(__file__).resolve().parent.parent
        overlay_dir = base_dir / "components" / "karpenter_aws" / "kustomize" / "overlays" / "ctrl"
        if not overlay_dir.exists():
            overlay_dir = (
                Path(sys.argv[-1]).resolve().parent.parent
                / "components"
                / "karpenter_aws"
                / "kustomize"
                / "overlays"
                / "ctrl"
            )
        rendered = subprocess.run(
            [self.kustomize, "build", str(overlay_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert rendered.returncode == 0, rendered.stderr
        resources = list(yaml.safe_load_all(rendered.stdout))
        nodepools = [r for r in resources if r and r.get("kind") == "NodePool"]
        assert len(nodepools) == 1
        control_pool = nodepools[0]
        assert control_pool["metadata"]["name"] == "control"

        requirements = control_pool["spec"]["template"]["spec"]["requirements"]
        cap_req = next(r for r in requirements if r["key"] == "karpenter.sh/capacity-type")
        assert cap_req["operator"] == "In"
        assert cap_req["values"] == ["on-demand"]
        assert "spot" not in cap_req["values"]

        labels = control_pool["spec"]["template"]["metadata"].get("labels", {})
        assert "profiling" not in labels

    def test_cloud_workload_controllers_cell_contracts(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        lb_comp = {
            "component": "aws-load-balancer-controller",
            "path": "src/infra/argocd/components/aws_load_balancer_controller/helm",
            "namespace": "kube-system",
            "wave": "20",
            "chart": "aws-load-balancer-controller",
            "chartRepo": "https://aws.github.io/eks-charts",
            "chartVersion": "3.5.0",
        }
        params_lb = self.parameters(lb_comp, "production")
        params_lb["metadata"]["labels"]["provider"] = "aws"
        params_lb["metadata"]["annotations"]["aws-region"] = "us-east-1"
        rendered_lb = self.render(app, [params_lb])
        assert rendered_lb.returncode == 0, rendered_lb.stderr
        lb_val = yaml.safe_load(rendered_lb.stdout)["spec"]["sources"][0]["helm"]["valuesObject"]
        assert lb_val["clusterName"] == "test-cluster"
        assert lb_val["region"] == "us-east-1"

        ext_dns_comp = {
            "component": "external-dns",
            "path": "src/infra/argocd/components/external_dns/kustomize",
            "namespace": "external-dns-system",
            "wave": "30",
            "chart": "external-dns",
            "chartRepo": "https://kubernetes-sigs.github.io/external-dns",
            "chartVersion": "1.21.1",
        }
        for prov, expected in (("aws", "aws"), ("gcp", "google")):
            with self.subTest(dns_provider=prov):
                params_dns = self.parameters(ext_dns_comp, "production")
                params_dns["metadata"]["labels"]["provider"] = prov
                rendered_dns = self.render(app, [params_dns])
                assert rendered_dns.returncode == 0, rendered_dns.stderr
                dns_val = yaml.safe_load(rendered_dns.stdout)["spec"]["sources"][0]["helm"][
                    "valuesObject"
                ]
                assert dns_val["provider"] == expected
                assert dns_val["txtOwnerId"] == "test-cluster"
                assert dns_val["domainFilters"] == ["c.corp.local.internal", "corp.local.internal"]

    def test_envoy_and_routing_cell_contracts(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        eg_comp = {
            "component": "envoy-gateway-instance",
            "path": "src/infra/argocd/components/envoy_gateway_instance/helm",
            "namespace": "envoy-gateway-system",
            "wave": "30",
            "helmValues": "values.yaml",
        }
        rr_comp = {
            "component": "routing-registry",
            "path": "src/infra/argocd/components/routing_registry/helm",
            "namespace": "envoy-gateway-system",
            "wave": "30",
            "helmValues": "values.yaml",
            "helmValueFiles": ["routes.yaml"],
            "injectClusterProvider": True,
        }
        routing_params = self.parameters(rr_comp, "production")
        routing_params["metadata"]["annotations"]["access-domain"] = "c.example.internal"
        routing_params["metadata"]["annotations"]["domain"] = "example.internal"
        rendered = self.render(app, [self.parameters(eg_comp, "production"), routing_params])
        assert rendered.returncode == 0, rendered.stderr
        patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
        eg_val = next(p for p in patches if p["metadata"]["name"] == "envoy-gateway-instance")[
            "spec"
        ]["sources"][0]["helm"]["valuesObject"]
        rr_val = next(p for p in patches if p["metadata"]["name"] == "routing-registry")["spec"][
            "sources"
        ][0]["helm"]["valuesObject"]
        assert eg_val["proxy"]["autoscaling"]["prometheus"]["cluster"] == "test-cluster"
        assert eg_val["gateway"]["baseDomain"] == "test-cluster.c.corp.local.internal"
        assert rr_val["gateway"]["baseDomain"] == "test-cluster.c.example.internal"
        assert rr_val["gatewayOIDC"]["issuer"] == "https://dex.example.internal"
        assert rr_val["gatewayOIDC"]["jwksUri"] == "https://dex.example.internal/keys"
        assert rr_val["target"]["name"] == "test-cluster"
        assert rr_val["target"]["provider"] == "floci"
        assert rr_val["target"]["role"] == "cell"

    def test_gateway_oidc_policies_use_registered_dex_endpoints(self) -> None:
        inputs = [Path(arg) for arg in " ".join(sys.argv[1:-3]).split()]
        routing_manifest = next(
            path for path in inputs if path.name == "helm_render-local-cell.yaml"
        )
        routing_resources = list(yaml.safe_load_all(routing_manifest.read_text()))
        ray_policy = next(
            resource
            for resource in routing_resources
            if resource
            and resource.get("kind") == "SecurityPolicy"
            and resource["metadata"]["name"] == "ray-dashboard-oidc"
        )
        ray_provider = ray_policy["spec"]["oidc"]["provider"]
        assert ray_provider["authorizationEndpoint"] == f"{ray_provider['issuer']}/auth"
        assert ray_provider["tokenEndpoint"] == f"{ray_provider['issuer']}/token"
        assert ray_provider["endSessionEndpoint"] == f"{ray_provider['issuer']}/logout"

        for app_name in ("cell-apps", "ctrl-apps"):
            with self.subTest(app=app_name):
                app = next(app for app in self.apps if app["metadata"]["name"] == app_name)
                components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"][
                    "elements"
                ]
                velero_ui = next(
                    component for component in components if component["component"] == "velero-ui"
                )
                parameters = self.parameters(velero_ui, "minimal")
                parameters["metadata"]["annotations"]["access-domain"] = "c.example.internal"
                parameters["metadata"]["annotations"]["domain"] = "example.internal"
                rendered = self.render(app, [parameters])
                assert rendered.returncode == 0, rendered.stderr
                application = yaml.safe_load(rendered.stdout)
                patches = [
                    patch
                    for source in application["spec"]["sources"]
                    for patch in source.get("kustomize", {}).get("patches", [])
                ]
                policy_patch = next(
                    patch
                    for patch in patches
                    if patch["target"].get("kind") == "SecurityPolicy"
                    and patch["target"].get("name") == "velero-ui-oidc"
                )
                operations = yaml.safe_load(policy_patch["patch"])
                values = {operation["path"]: operation["value"] for operation in operations}
                issuer = "https://dex.example.internal"
                assert values["/spec/oidc/provider/issuer"] == issuer
                assert values["/spec/oidc/provider/authorizationEndpoint"] == f"{issuer}/auth"
                assert values["/spec/oidc/provider/endSessionEndpoint"] == f"{issuer}/logout"
                assert values["/spec/jwt/providers/0/issuer"] == issuer
                if app_name == "ctrl-apps":
                    # The control cluster reaches its own Dex in-cluster; the public
                    # issuer host may serve a certificate outside the local trust bundle.
                    assert values["/spec/oidc/provider/tokenEndpoint"] == (
                        "http://dex.dex.svc.cluster.local:5556/token"
                    )
                    assert values["/spec/jwt/providers/0/remoteJWKS/uri"] == (
                        "http://dex.dex.svc.cluster.local:5556/keys"
                    )
                else:
                    assert values["/spec/oidc/provider/tokenEndpoint"] == f"{issuer}/token"

    def test_dex_config_uses_registered_domains(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        dex_comp = next(c for c in components if c["component"] == "dex")
        params = self.parameters(dex_comp, "minimal")
        params["metadata"]["annotations"]["access-domain"] = "c.custom.corp"
        params["metadata"]["annotations"]["domain"] = "custom.corp"
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        application = yaml.safe_load(rendered.stdout)
        patches = [
            patch
            for source in application["spec"]["sources"]
            for patch in source.get("kustomize", {}).get("patches", [])
        ]
        dex_patch = next(
            patch
            for patch in patches
            if patch["target"].get("kind") == "Secret"
            and patch["target"].get("name") == "dex-config"
        )
        parsed_secret = yaml.safe_load(dex_patch["patch"])
        dex_config = yaml.safe_load(parsed_secret["stringData"]["config.yaml"])
        assert dex_config["issuer"] == "https://dex.custom.corp"
        argocd_client = next(c for c in dex_config["staticClients"] if c["id"] == "argocd")
        assert argocd_client["redirectURIs"] == ["https://argocd.custom.corp/auth/callback"]
        assert argocd_client["secretEnv"] == "ARGOCD_CLIENT_SECRET"
        assert "secret" not in argocd_client

    def _dex_rendered(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        rendered = self.render(app, [params])
        assert rendered.returncode == 0, rendered.stderr
        child = yaml.safe_load(rendered.stdout)
        patches = [
            patch
            for source in child["spec"]["sources"]
            for patch in source.get("kustomize", {}).get("patches", [])
        ]
        dex_patch = next(
            p
            for p in patches
            if p["target"].get("kind") == "Secret" and p["target"].get("name") == "dex-config"
        )
        parsed_secret = yaml.safe_load(dex_patch["patch"])
        dex_config = yaml.safe_load(parsed_secret["stringData"]["config.yaml"])
        return child, patches, dex_config

    def _dex_cloud_params(self) -> dict[str, Any]:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        dex_comp = next(c for c in components if c["component"] == "dex")
        params = self.parameters(dex_comp, "production")
        params["metadata"]["labels"]["provider"] = "aws"
        params["metadata"]["annotations"]["public-domain"] = "boltz.bio"
        params["metadata"]["annotations"]["hosted-domains"] = "boltz.bio,boltz.com"
        params["metadata"]["annotations"]["admin-email"] = "admin@boltz.bio"
        return params

    def test_dex_google_connector_groups_disabled_by_default(self) -> None:
        for groups_flag in (None, "false"):
            with self.subTest(groups_flag=groups_flag):
                params = self._dex_cloud_params()
                if groups_flag is not None:
                    params["metadata"]["annotations"]["google-groups-enabled"] = groups_flag
                child, patches, dex_config = self._dex_rendered(params)
                assert len(dex_config["connectors"]) == 1
                connector = dex_config["connectors"][0]
                assert connector["type"] == "google"
                assert connector["id"] == "google"
                assert connector["config"]["clientID"] == "$GOOGLE_CLIENT_ID"
                assert connector["config"]["clientSecret"] == "$GOOGLE_CLIENT_SECRET"
                assert connector["config"]["redirectURI"] == "https://dex.corp.boltz.bio/callback"
                assert connector["config"]["hostedDomains"] == ["boltz.bio", "boltz.com"]
                assert "serviceAccountFilePath" not in connector["config"]
                assert "adminEmail" not in connector["config"]
                assert "groups" not in connector["config"]
                for source in child["spec"]["sources"]:
                    helm_values = source.get("helm", {}).get("valuesObject", {})
                    assert not any(
                        vm.get("name") == "google-sa" for vm in helm_values.get("volumeMounts", [])
                    )
                    assert not any(
                        v.get("name") == "google-sa" for v in helm_values.get("volumes", [])
                    )
                es_patch = next(
                    p
                    for p in patches
                    if p["target"].get("kind") == "ExternalSecret"
                    and p["target"].get("name") == "google-workspace-oauth"
                )
                assert yaml.safe_load(es_patch["patch"]) == [
                    {
                        "op": "replace",
                        "path": "/spec/secretStoreRef/name",
                        "value": "aws-secrets-manager",
                    },
                    {
                        "op": "replace",
                        "path": "/spec/dataFrom/0/extract/key",
                        "value": "test-cluster-google-workspace-oauth",
                    },
                ]

    def test_dex_google_connector_groups_enabled_by_annotation(self) -> None:
        params = self._dex_cloud_params()
        params["metadata"]["annotations"]["google-groups-enabled"] = "true"
        child, _patches, dex_config = self._dex_rendered(params)
        connector = dex_config["connectors"][0]
        assert (
            connector["config"]["serviceAccountFilePath"]
            == "/etc/dex/google-sa/service-account.json"
        )
        assert connector["config"]["adminEmail"] == "admin@boltz.bio"
        assert connector["config"]["groups"] == [
            "operators",
            "operators@boltz.bio",
            "operators@boltz.com",
        ]
        assert connector["config"]["hostedDomains"] == ["boltz.bio", "boltz.com"]
        helm_values = next(
            s["helm"]["valuesObject"]
            for s in child["spec"]["sources"]
            if "helm" in s and "valuesObject" in s["helm"]
        )
        assert any(vm.get("name") == "google-sa" for vm in helm_values.get("volumeMounts", []))
        assert any(v.get("name") == "google-sa" for v in helm_values.get("volumes", []))

    def test_dex_google_connector_groups_enabled_by_helm_value(self) -> None:
        for flag_key in ("google-groups-enabled", "googleGroupsEnabled"):
            with self.subTest(helm_flag=flag_key):
                params = self._dex_cloud_params()
                params.setdefault("helmValuesObject", {})[flag_key] = True
                _child, _patches, dex_config = self._dex_rendered(params)
                conn = dex_config["connectors"][0]
                assert (
                    conn["config"]["serviceAccountFilePath"]
                    == "/etc/dex/google-sa/service-account.json"
                )
                assert conn["config"]["adminEmail"] == "admin@boltz.bio"
                assert conn["config"]["groups"] == [
                    "operators",
                    "operators@boltz.bio",
                    "operators@boltz.com",
                ]

        params_override = self._dex_cloud_params()
        params_override["metadata"]["annotations"]["google-groups-enabled"] = "false"
        params_override.setdefault("helmValuesObject", {})["googleGroupsEnabled"] = True
        _child, _patches, config_override = self._dex_rendered(params_override)
        conn_override = config_override["connectors"][0]
        assert "serviceAccountFilePath" not in conn_override["config"]
        assert "adminEmail" not in conn_override["config"]
        assert "groups" not in conn_override["config"]

    def test_operator_identity_contracts(self) -> None:
        for app_name in ("cell-apps", "ctrl-apps"):
            with self.subTest(app=app_name):
                app = next(app for app in self.apps if app["metadata"]["name"] == app_name)
                components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"][
                    "elements"
                ]
                access_comp = next(c for c in components if c["component"] == "cluster-access")
                params = self.parameters(access_comp, "minimal")
                params["metadata"]["labels"]["provider"] = "aws"
                params["metadata"]["annotations"]["operator-emails"] = "simone@boltz.bio"
                params["metadata"]["annotations"]["hosted-domains"] = "boltz.bio,boltz.com"
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                application = yaml.safe_load(rendered.stdout)
                patches = [
                    patch
                    for source in application["spec"]["sources"]
                    for patch in source.get("kustomize", {}).get("patches", [])
                ]
                binding_patches = [
                    patch
                    for patch in patches
                    if patch["target"].get("kind") == "ClusterRoleBinding"
                    and patch["target"].get("name") == "cluster-operators"
                ]
                subjects = []
                for bp in binding_patches:
                    operations = yaml.safe_load(bp["patch"])
                    subjects.extend([
                        op["value"] for op in operations if op.get("path") == "/subjects/-"
                    ])
                assert any(
                    s.get("kind") == "User" and s.get("name") == "cluster:user:simone@boltz.bio"
                    for s in subjects
                )
                assert any(
                    s.get("kind") == "Group"
                    and s.get("name") == "cluster:group:operators@boltz.bio"
                    for s in subjects
                )

        ctrl_app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        ctrl_comps = ctrl_app["spec"]["generators"][0]["matrix"]["generators"][1]["list"][
            "elements"
        ]
        dragonfly_comp = next(c for c in ctrl_comps if c["component"] == "dragonfly-manager")
        params = self.parameters(dragonfly_comp, "minimal")
        params["metadata"]["labels"]["provider"] = "aws"
        params["metadata"]["annotations"]["ecr-registry"] = (
            "111122223333.dkr.ecr.us-west-2.amazonaws.com"
        )
        params["metadata"]["annotations"]["operator-emails"] = "simone@boltz.bio"
        params["metadata"]["annotations"]["hosted-domains"] = "boltz.bio,boltz.com"
        rendered = self.render(ctrl_app, [params])
        assert rendered.returncode == 0, rendered.stderr
        application = yaml.safe_load(rendered.stdout)
        patches = [
            patch
            for source in application["spec"]["sources"]
            for patch in source.get("kustomize", {}).get("patches", [])
        ]
        sec_patch = next(
            patch
            for patch in patches
            if patch["target"].get("kind") == "SecurityPolicy"
            and patch["target"].get("name") == "dragonfly-console-oidc"
        )
        operations = yaml.safe_load(sec_patch["patch"])
        rule_op = next(op for op in operations if op.get("path") == "/spec/authorization/rules/-")
        assert rule_op["value"]["name"] == "operator-email"
        assert rule_op["value"]["principal"]["jwt"]["claims"][0]["values"] == ["simone@boltz.bio"]

    def test_velero_provider_contracts(self) -> None:
        for app_name in ("cell-apps", "ctrl-apps"):
            app = next(app for app in self.apps if app["metadata"]["name"] == app_name)
            components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
            velero_comp = next(item for item in components if item["component"] == "velero")

            # Floci
            params_floci = self.parameters(velero_comp, "minimal")
            params_floci["metadata"]["labels"]["provider"] = "floci"
            rendered_floci = self.render(app, [params_floci])
            assert rendered_floci.returncode == 0, rendered_floci.stderr
            floci_helm = yaml.safe_load(rendered_floci.stdout)["spec"]["sources"][0]["helm"]
            floci_val = floci_helm["valuesObject"]
            assert floci_helm["valueFiles"][-1].endswith("velero/helm/providers/floci.yaml")
            assert "credentials" not in floci_val
            assert floci_val["configuration"]["backupStorageLocation"][0]["provider"] == "aws"
            assert (
                floci_val["configuration"]["backupStorageLocation"][0]["bucket"]
                == "foundation-test-backups"
            )
            assert (
                floci_val["configuration"]["backupStorageLocation"][0]["config"]["s3Url"]
                == "http://172.19.0.2:4566"
            )

            assert (
                floci_val["configuration"]["backupStorageLocation"][0]["prefix"]
                == "backups/cluster"
            )
            del params_floci["metadata"]["annotations"]["backups-bucket"]
            missing_bucket = self.render(app, [params_floci])
            assert missing_bucket.returncode != 0
            assert "requires the backups-bucket cluster annotation" in missing_bucket.stderr

            # AWS
            params_aws = self.parameters(velero_comp, "production")
            params_aws["metadata"]["labels"]["provider"] = "aws"
            params_aws["metadata"]["annotations"]["aws-region"] = "us-west-2"
            params_aws["metadata"]["annotations"].pop("backups-bucket", None)
            rendered_aws = self.render(app, [params_aws])
            assert rendered_aws.returncode == 0, rendered_aws.stderr
            aws_val = yaml.safe_load(rendered_aws.stdout)["spec"]["sources"][0]["helm"][
                "valuesObject"
            ]
            assert not aws_val["credentials"]["useSecret"]
            assert aws_val["configuration"]["backupStorageLocation"][0]["provider"] == "aws"
            assert (
                aws_val["configuration"]["backupStorageLocation"][0]["bucket"]
                == "corp-test-cluster-backups"
            )
            assert (
                aws_val["configuration"]["backupStorageLocation"][0]["config"]["region"]
                == "us-west-2"
            )

            # AWS with custom resource-prefix
            params_aws_custom = self.parameters(velero_comp, "production")
            params_aws_custom["metadata"]["labels"]["provider"] = "aws"
            params_aws_custom["metadata"]["annotations"]["aws-region"] = "us-west-2"
            params_aws_custom["metadata"]["annotations"]["resource-prefix"] = "customcorp"
            params_aws_custom["metadata"]["annotations"].pop("backups-bucket", None)
            rendered_aws_custom = self.render(app, [params_aws_custom])
            assert rendered_aws_custom.returncode == 0, rendered_aws_custom.stderr
            aws_custom_val = yaml.safe_load(rendered_aws_custom.stdout)["spec"]["sources"][0][
                "helm"
            ]["valuesObject"]
            assert (
                aws_custom_val["configuration"]["backupStorageLocation"][0]["bucket"]
                == "customcorp-test-cluster-backups"
            )

            # GCP
            params_gcp = self.parameters(velero_comp, "production")
            params_gcp["metadata"]["labels"]["provider"] = "gcp"
            params_gcp["metadata"]["annotations"]["gcp-project-id"] = "my-gcp-project"
            params_gcp["metadata"]["annotations"].pop("backups-bucket", None)
            rendered_gcp = self.render(app, [params_gcp])
            assert rendered_gcp.returncode == 0, rendered_gcp.stderr
            gcp_val = yaml.safe_load(rendered_gcp.stdout)["spec"]["sources"][0]["helm"][
                "valuesObject"
            ]
            assert not gcp_val["credentials"]["useSecret"]
            assert gcp_val["configuration"]["backupStorageLocation"][0]["provider"] == "gcp"
            assert (
                gcp_val["configuration"]["backupStorageLocation"][0]["bucket"]
                == "corp-test-cluster-backups"
            )
            assert (
                gcp_val["serviceAccount"]["server"]["annotations"]["iam.gke.io/gcp-service-account"]
                == "test-cluster-velero@my-gcp-project.iam.gserviceaccount.com"
            )

            if app_name == "ctrl-apps":
                params_override = self.parameters(velero_comp, "production")
                params_override["metadata"]["labels"]["provider"] = "aws"
                params_override["metadata"]["annotations"]["aws-region"] = "us-west-2"
                params_override["metadata"]["annotations"]["backups-bucket"] = (
                    "custom-backup-bucket"
                )
                rendered_override = self.render(app, [params_override])
                assert rendered_override.returncode == 0, rendered_override.stderr
                override_val = yaml.safe_load(rendered_override.stdout)["spec"]["sources"][0][
                    "helm"
                ]["valuesObject"]
                assert (
                    override_val["configuration"]["backupStorageLocation"][0]["bucket"]
                    == "custom-backup-bucket"
                )

    def test_local_ctrl_dns_and_routing_contracts(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        floci_generator = next(
            generator
            for generator in app["spec"]["generators"]
            if generator["matrix"]["generators"][0]["matrix"]["generators"][0]["clusters"][
                "selector"
            ]["matchLabels"].get("provider")
            == "floci"
        )
        components = floci_generator["matrix"]["generators"][1]["list"]["elements"]
        coredns = next(component for component in components if component["component"] == "coredns")
        rendered_coredns = self.render(app, [self.parameters(coredns, "minimal")])
        assert rendered_coredns.returncode == 0, rendered_coredns.stderr
        coredns_values = yaml.safe_load(rendered_coredns.stdout)["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        assert (
            coredns_values["privateAccess"]["baseDomain"]
            == f"{coredns_values['cluster']['name']}.{coredns_values['internalDomain']}"
        )

        routing = {
            "component": "routing-registry",
            "path": "src/infra/argocd/components/routing_registry/helm",
            "namespace": "envoy-gateway-system",
            "wave": "30",
            "helmValues": "values.yaml",
            "helmValueFiles": ["routes.yaml"],
            "injectClusterProvider": True,
        }
        rendered_routing = self.render(app, [self.parameters(routing, "minimal")])
        assert rendered_routing.returncode == 0, rendered_routing.stderr
        routing_values = yaml.safe_load(rendered_routing.stdout)["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        assert routing_values["target"]["name"] == "test-cluster"
        assert routing_values["target"]["provider"] == "floci"
        assert routing_values["target"]["role"] == "ctrl"

    def test_ctrl_public_hooks_route_listener_and_dns(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        generators = app["spec"]["generators"][0]["matrix"]["generators"]
        elements = generators[1]["list"]["elements"]

        # 1. Listener assertions on envoy-gateway-instance-public-hooks
        eg_comp = next(
            c for c in elements if c["component"] == "envoy-gateway-instance-public-hooks"
        )
        assert eg_comp["helmValuesObject"]["gateway"]["name"] == "public-hooks"
        assert eg_comp["helmValuesObject"]["proxy"]["service"]["type"] == "LoadBalancer"
        assert (
            eg_comp["helmValuesObject"]["proxy"]["service"]["loadBalancerClass"]
            == "service.k8s.aws/nlb"
        )
        assert (
            eg_comp["helmValuesObject"]["proxy"]["service"]["annotations"][
                "service.beta.kubernetes.io/aws-load-balancer-ip-address-type"
            ]
            == "dualstack"
        )
        assert (
            eg_comp["helmValuesObject"]["proxy"]["service"]["annotations"][
                "service.beta.kubernetes.io/aws-load-balancer-scheme"
            ]
            == "internet-facing"
        )
        assert (
            eg_comp["helmValuesObject"]["proxy"]["service"]["annotations"][
                "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type"
            ]
            == "ip"
        )
        eg_listeners = eg_comp["helmValuesObject"]["gateway"]["listeners"]
        assert eg_listeners["atlantisWebhookHttps"]["enabled"] is True
        assert (
            eg_listeners["atlantisWebhookHttps"]["certificateSecret"]
            == "public-hooks-atlantis-webhook-tls"
        )

        eg_params = self.parameters(eg_comp, "production")
        eg_params["metadata"]["annotations"]["public-domain"] = "boltz.com"
        rendered_eg = self.render(app, [eg_params])
        assert rendered_eg.returncode == 0, rendered_eg.stderr
        eg_val = yaml.safe_load(rendered_eg.stdout)["spec"]["sources"][0]["helm"]["valuesObject"]
        assert (
            eg_val["gateway"]["listeners"]["atlantisWebhookHttps"]["hostname"] == "hooks.boltz.com"
        )

        # Verify private-access Gateway no longer contains the hooks listener
        priv_comp = next(c for c in elements if c["component"] == "envoy-gateway-instance")
        assert "atlantisWebhookHttps" not in priv_comp["helmValuesObject"]["gateway"]["listeners"]

        # 2. DNS assertions on external-dns
        ext_dns_comp = {
            "component": "external-dns",
            "tier": "fleet",
            "path": "src/infra/argocd/components/external_dns/kustomize",
            "namespace": "external-dns-system",
            "wave": "30",
            "chart": "external-dns",
            "chartRepo": "https://kubernetes-sigs.github.io/external-dns",
            "chartVersion": "1.22.0",
        }
        ext_dns_params = self.parameters(ext_dns_comp, "production")
        ext_dns_params["metadata"]["labels"]["provider"] = "aws"
        ext_dns_params["metadata"]["annotations"]["public-domain"] = "boltz.com"
        rendered_ext_dns = self.render(app, [ext_dns_params])
        assert rendered_ext_dns.returncode == 0, rendered_ext_dns.stderr
        ext_dns_val = yaml.safe_load(rendered_ext_dns.stdout)["spec"]["sources"][0]["helm"][
            "valuesObject"
        ]
        assert "boltz.com" in ext_dns_val["domainFilters"]

        # 3. Route assertions on routing-registry-public
        rr_public_comp = next(c for c in elements if c["component"] == "routing-registry-public")
        assert rr_public_comp["helmValuesObject"]["exposure"] == "public"
        assert rr_public_comp["helmValuesObject"]["phase"] == "application"

        rr_params = self.parameters(rr_public_comp, "production")
        rr_params["metadata"]["labels"]["provider"] = "aws"
        rr_params["metadata"]["annotations"]["public-domain"] = "boltz.com"
        rendered_rr = self.render(app, [rr_params])
        assert rendered_rr.returncode == 0, rendered_rr.stderr
        rr_val = yaml.safe_load(rendered_rr.stdout)["spec"]["sources"][0]["helm"]["valuesObject"]
        assert rr_val["gateway"]["baseDomain"] == "boltz.com"
        assert rr_val["gateway"]["aliasDomain"] == ""
        assert rr_val["gateway"]["name"] == "public-hooks"
        assert rr_val["gateway"]["routeSections"]["atlantis-webhook"] == "atlantis-webhook-https"
        assert rr_val["exposure"] == "public"
        assert rr_val["target"]["role"] == "ctrl"

    def test_argocd_webhook_route_and_secret(self) -> None:
        base_dir = Path(__file__).resolve().parent.parent
        if not (base_dir / "components").exists():
            base_dir = Path(sys.argv[-1]).resolve().parent.parent

        # 1. Assert route entrypoint in routes.yaml
        routes_file = base_dir / "components" / "routing_registry" / "helm" / "routes.yaml"
        if routes_file.exists():
            routes_data = yaml.safe_load(routes_file.read_text(encoding="utf-8"))
            atlantis_route = next(
                r for r in routes_data["routes"] if r["name"] == "atlantis-webhook"
            )
            entrypoints = {ep["path"]: ep for ep in atlantis_route["entrypoints"]}
            assert "/github/argocd" in entrypoints
            argocd_ep = entrypoints["/github/argocd"]
            assert argocd_ep["rewrite"] == "/api/webhook"
            assert argocd_ep["backend"] == {
                "namespace": "argocd",
                "service": "argocd-server",
                "port": 80,
            }

        # 2. Assert absence of any literal secret and proper ExternalSecret projection
        es_file = (
            base_dir
            / "components"
            / "external_secrets"
            / "kustomize"
            / "argocd-webhook.external-secret.yaml"
        )
        if es_file.exists():
            es_data = yaml.safe_load(es_file.read_text(encoding="utf-8"))
            assert es_data["metadata"]["name"] == "argocd-webhook"
            assert es_data["metadata"]["namespace"] == "argocd"
            assert es_data["spec"]["target"]["name"] == "argocd-secret"
            assert es_data["spec"]["target"]["creationPolicy"] == "Merge"
            assert es_data["spec"]["target"]["deletionPolicy"] == "Retain"
            assert len(es_data["spec"]["data"]) == 1
            data_spec = es_data["spec"]["data"][0]
            assert data_spec["secretKey"] == "webhook.github.secret"
            assert data_spec["remoteRef"]["key"] == "argocd-github-webhook"
            assert data_spec["remoteRef"]["property"] == "webhook_secret"
            # Verify no literal value or secret is present in ExternalSecret
            assert "value" not in data_spec
            assert "stringData" not in es_data

    def test_argocd_webhook_cluster_patches(self) -> None:
        # 1. Assert cell clusters delete argocd-webhook ExternalSecret
        cell_app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        cell_generators = cell_app["spec"]["generators"][0]["matrix"]["generators"]
        cell_elements = cell_generators[1]["list"]["elements"]
        cell_es_comp = next(c for c in cell_elements if c["component"] == "external-secrets")
        cell_params = self.parameters(cell_es_comp, "production")
        rendered_cell_es = self.render(cell_app, [cell_params])
        assert rendered_cell_es.returncode == 0, rendered_cell_es.stderr
        cell_patches = yaml.safe_load(rendered_cell_es.stdout)["spec"]["sources"][2]["kustomize"][
            "patches"
        ]
        assert any(
            p.get("target", {}).get("name") == "argocd-webhook"
            and "$patch: delete" in p.get("patch", "")
            for p in cell_patches
        )

        # 2. Assert ctrl cluster on Floci patches argocd-webhook to local-secret-records
        ctrl_app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        ctrl_generators = ctrl_app["spec"]["generators"][0]["matrix"]["generators"]
        ctrl_elements = ctrl_generators[1]["list"]["elements"]
        ctrl_es_comp = next(c for c in ctrl_elements if c["component"] == "external-secrets")
        floci_params = self.parameters(ctrl_es_comp, "minimal")
        floci_params["metadata"]["labels"]["provider"] = "floci"
        rendered_floci_es = self.render(ctrl_app, [floci_params])
        assert rendered_floci_es.returncode == 0, rendered_floci_es.stderr
        floci_patches = yaml.safe_load(rendered_floci_es.stdout)["spec"]["sources"][2]["kustomize"][
            "patches"
        ]
        assert any(
            p.get("target", {}).get("name") == "argocd-webhook"
            and "local-secret-records" in p.get("patch", "")
            for p in floci_patches
        )

        # 3. Assert cloud ctrl cluster patches argocd-webhook to aws-secrets-manager
        cloud_params = self.parameters(ctrl_es_comp, "production")
        cloud_params["metadata"]["labels"]["provider"] = "aws"
        cloud_params["nameNormalized"] = "ctrl-aws-usw2"
        rendered_cloud_es = self.render(ctrl_app, [cloud_params])
        assert rendered_cloud_es.returncode == 0, rendered_cloud_es.stderr
        cloud_patches = yaml.safe_load(rendered_cloud_es.stdout)["spec"]["sources"][2]["kustomize"][
            "patches"
        ]
        assert any(
            p.get("target", {}).get("name") == "argocd-webhook"
            and "aws-secrets-manager" in p.get("patch", "")
            and "ctrl-aws-usw2-argocd-github-webhook" in p.get("patch", "")
            for p in cloud_patches
        )

    def test_atlantis_uses_registered_secret_store(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        component = next(item for item in components if item["component"] == "atlantis")
        for store in ("local-secret-records", "runtime-secrets"):
            with self.subTest(store=store):
                params = self.parameters(component, "minimal")
                params["metadata"]["annotations"]["secret-store"] = store
                rendered = self.render(app, [params])
                assert rendered.returncode == 0, rendered.stderr
                patches = yaml.safe_load(rendered.stdout)["spec"]["sources"][2]["kustomize"][
                    "patches"
                ]
                secret_patches = [
                    patch for patch in patches if patch["target"].get("kind") == "ExternalSecret"
                ]
                assert {patch["target"]["name"] for patch in secret_patches} == {
                    "atlantis-github-app",
                    "atlantis-runtime-environment",
                }
                assert all(
                    yaml.safe_load(patch["patch"])[0]["value"] == store for patch in secret_patches
                )
        params["metadata"]["annotations"].pop("secret-store")
        assert self.render(app, [params]).returncode != 0

        # Cloud ctrl cluster patches atlantis-github-app to aws-secrets-manager
        cloud_params = self.parameters(component, "production")
        cloud_params["metadata"]["labels"]["provider"] = "aws"
        cloud_params["nameNormalized"] = "ctrl-aws-usw2"
        cloud_rendered = self.render(app, [cloud_params])
        assert cloud_rendered.returncode == 0, cloud_rendered.stderr
        cloud_patches = yaml.safe_load(cloud_rendered.stdout)["spec"]["sources"][2]["kustomize"][
            "patches"
        ]
        assert any(
            p.get("target", {}).get("name") == "atlantis-github-app"
            and "aws-secrets-manager" in p.get("patch", "")
            and "ctrl-aws-usw2-atlantis-github-app" in p.get("patch", "")
            for p in cloud_patches
        )

    def test_default_vpa_source_renders_only_for_declared_targets(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        undeclared_comp = {
            "component": "opencost",
            "path": "src/infra/argocd/components/opencost/kustomize",
            "namespace": "opencost",
            "wave": "40",
            "chart": "opencost",
            "chartRepo": "https://opencost.github.io/opencost-helm-chart",
            "chartVersion": "2.5.31",
        }
        enabled_comp = {
            "component": "coredns",
            "path": "src/infra/argocd/components/coredns/helm",
            "namespace": "kube-system",
            "wave": "5",
            "helmValues": "values.yaml",
            "vpa": True,
        }
        multi_comp = {
            "component": "cert-manager",
            "path": "src/infra/argocd/components/cert_manager/kustomize",
            "namespace": "cert-manager-system",
            "wave": "10",
            "chart": "cert-manager",
            "chartRepo": "https://charts.jetstack.io",
            "chartVersion": "v1.21.2",
            "vpa": {
                "targets": [
                    "cert-manager",
                    "cert-manager-cainjector",
                    "cert-manager-webhook",
                ]
            },
        }
        disabled_comp = {
            "component": "scheduling-priorities",
            "path": "src/infra/argocd/components/scheduling_priorities/kustomize",
            "namespace": "kube-system",
            "wave": "10",
            "vpa": False,
        }
        custom_comp = {
            "component": "atlantis",
            "path": "src/infra/argocd/components/atlantis/kustomize",
            "namespace": "atlantis",
            "wave": "40",
            "chart": "atlantis",
            "chartRepo": "https://runatlantis.github.io/helm-charts",
            "chartVersion": "6.15.1",
            "vpa": "custom",
        }
        rendered = self.render(
            app,
            [
                self.parameters(undeclared_comp, "production"),
                self.parameters(enabled_comp, "production"),
                self.parameters(multi_comp, "production"),
                self.parameters(disabled_comp, "production"),
                self.parameters(custom_comp, "production"),
            ],
        )
        assert rendered.returncode == 0, rendered.stderr
        patches = list(yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader))
        patch_by_name = {p["metadata"]["name"]: p for p in patches}

        # opencost: no vpa key -> no default_vpa source; Kyverno covers its workloads
        for src in patch_by_name["opencost"]["spec"]["sources"]:
            assert src.get("path") != "src/infra/argocd/components/default_vpa/helm"

        # coredns: vpa: true -> single target named after the component
        vpa_source_enabled = patch_by_name["coredns"]["spec"]["sources"][-1]
        assert vpa_source_enabled["path"] == "src/infra/argocd/components/default_vpa/helm"
        assert vpa_source_enabled["helm"]["valuesObject"]["targets"] == [{"name": "coredns"}]

        # cert-manager: multi targets
        vpa_source_multi = patch_by_name["cert-manager"]["spec"]["sources"][-1]
        assert vpa_source_multi["path"] == "src/infra/argocd/components/default_vpa/helm"
        assert vpa_source_multi["helm"]["valuesObject"]["targets"] == [
            {"name": "cert-manager"},
            {"name": "cert-manager-cainjector"},
            {"name": "cert-manager-webhook"},
        ]

        # scheduling-priorities: disabled (vpa: false) -> no default_vpa source
        for src in patch_by_name["scheduling-priorities"]["spec"]["sources"]:
            assert src.get("path") != "src/infra/argocd/components/default_vpa/helm"

        # atlantis: custom VPA -> no default_vpa source
        for src in patch_by_name["atlantis"]["spec"]["sources"]:
            assert src.get("path") != "src/infra/argocd/components/default_vpa/helm"

    def test_vpa_resources_have_one_owner(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        selected = [
            component for component in components if component["component"] in {"coder", "vpa"}
        ]
        rendered = self.render(
            app, [self.parameters(component, "minimal") for component in selected]
        )
        assert rendered.returncode == 0, rendered.stderr
        applications = {
            item["metadata"]["name"]: item
            for item in yaml.load_all(rendered.stdout, Loader=UniqueKeyLoader)
        }

        coder_sources = applications["coder"]["spec"]["sources"]
        assert coder_sources[-1]["helm"]["valuesObject"]["targets"] == [
            {"name": "coder-oidc-entrypoint"}
        ]

        vpa_sources = applications["vpa"]["spec"]["sources"]
        assert "skipCrds" not in vpa_sources[0]["helm"]

    def test_single_replica_pod_disruption_budgets_allow_disruptions(self) -> None:
        """Reject PodDisruptionBudgets that allow zero disruptions for single-replica workloads."""
        inputs = [Path(arg) for arg in " ".join(sys.argv[1:-3]).split()]
        single_replica_selectors = [
            {"app.kubernetes.io/name": "altinity-clickhouse-operator"},
            {"app.kubernetes.io/name": "signoz", "app.kubernetes.io/component": "signoz"},
            {"app.kubernetes.io/name": "homer"},
            {"app.kubernetes.io/name": "parca"},
        ]
        errors: list[str] = []
        for path in inputs:
            if not path.is_file() or not path.name.endswith(".yaml"):
                continue
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            for doc in documents:
                if not isinstance(doc, dict) or doc.get("kind") != "PodDisruptionBudget":
                    continue
                match_labels = doc.get("spec", {}).get("selector", {}).get("matchLabels", {})
                for selector in single_replica_selectors:
                    if selector.items() <= match_labels.items():
                        min_avail = doc.get("spec", {}).get("minAvailable")
                        if min_avail is not None and int(min_avail) >= 1:
                            errors.append(
                                f"{path.name} PDB {doc.get('metadata', {}).get('name')} specifies "
                                f"minAvailable: {min_avail} for single-replica workload "
                                f"{selector}; must use maxUnavailable: 1"
                            )
        assert not errors, "\n".join(errors)

    def test_cloud_external_secrets_follow_cluster_prefix_naming(self) -> None:
        """Verify cloud ExternalSecrets targeting aws-secrets-manager use unified <cluster>- prefix."""
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]

        # 1. Test ACME cloudflare-api-token patches to cluster prefix on cloud
        acme_comp = next((c for c in components if c["component"] == "acme"), None)
        if acme_comp:
            params = self.parameters(acme_comp, "production")
            params["metadata"]["labels"]["provider"] = "aws"
            params["metadata"]["labels"]["acme-dns01"] = "enabled"
            params["nameNormalized"] = "ctrl-aws-usw2"
            rendered = self.render(app, [params])
            assert rendered.returncode == 0, rendered.stderr
            application = yaml.safe_load(rendered.stdout)
            cf_patches = [
                p
                for source in application["spec"]["sources"]
                for p in source.get("kustomize", {}).get("patches", [])
                if p["target"].get("name") == "cloudflare-api-token"
            ]
            assert len(cf_patches) == 1
            ops = {op["path"]: op["value"] for op in yaml.safe_load(cf_patches[0]["patch"])}
            assert ops["/spec/data/0/remoteRef/key"] == "ctrl-aws-usw2-cloudflare-api-token"

            issuer_patches = [
                p
                for source in application["spec"]["sources"]
                for p in source.get("kustomize", {}).get("patches", [])
                if p["target"].get("name") == "public-acme"
            ]
            assert len(issuer_patches) == 1
            issuer_ops = {
                op["path"]: op["value"] for op in yaml.safe_load(issuer_patches[0]["patch"])
            }
            assert "/spec/acme/email" in issuer_ops
            assert "/spec/acme/solvers/0/selector/dnsZones" in issuer_ops
            assert "/spec/acme/solvers/1/selector/dnsZones" in issuer_ops

        # 2. Test tailscale_access defaults to unified cluster prefix on cloud
        ts_comp = next((c for c in components if c["component"] == "tailscale-access"), None)
        if ts_comp:
            params = self.parameters(ts_comp, "production")
            params["metadata"]["labels"]["provider"] = "aws"
            params["nameNormalized"] = "ctrl-aws-usw2"
            rendered = self.render(app, [params])
            assert rendered.returncode == 0, rendered.stderr
            application = yaml.safe_load(rendered.stdout)
            source = next(s for s in application["spec"]["sources"] if "helm" in s)
            ts_values = source["helm"]["valuesObject"]
            assert ts_values["cloud"]["oauthRemoteKey"] == "ctrl-aws-usw2-tailscale-operator-oauth"

    def test_chainsaw_storage_and_scheduling_deploy_only_on_cells(self) -> None:
        """Verify chainsaw-storage and chainsaw-scheduling checks and tests deploy only on cells."""
        ctrl_app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        ctrl_components = ctrl_app["spec"]["generators"][0]["matrix"]["generators"][1]["list"][
            "elements"
        ]
        ctrl_kh = next(c for c in ctrl_components if c["component"] == "kuberhealthy")
        ctrl_rendered = self.render(ctrl_app, [self.parameters(ctrl_kh, "minimal")])
        assert ctrl_rendered.returncode == 0, ctrl_rendered.stderr
        ctrl_app_spec = yaml.safe_load(ctrl_rendered.stdout)
        ctrl_patches = ctrl_app_spec["spec"]["sources"][2]["kustomize"]["patches"]
        ctrl_deleted = {
            p.get("target", {}).get("name")
            for p in ctrl_patches
            if "$patch: delete" in p.get("patch", "")
        }
        assert "chainsaw-scheduling" in ctrl_deleted
        assert "chainsaw-scheduling-tests" in ctrl_deleted
        assert "chainsaw-storage" in ctrl_deleted
        assert "chainsaw-storage-tests" in ctrl_deleted

        ctrl_deleted_resources = {
            (p.get("target", {}).get("kind"), p.get("target", {}).get("name"))
            for p in ctrl_patches
            if "$patch: delete" in p.get("patch", "")
        }
        for name in ("chainsaw-scheduling", "chainsaw-storage"):
            for kind in (
                "ServiceAccount",
                "ClusterRole",
                "ClusterRoleBinding",
                "Role",
                "RoleBinding",
                "KuberhealthyCheck",
            ):
                assert (kind, name) in ctrl_deleted_resources
        assert ("ConfigMap", "chainsaw-scheduling-tests") in ctrl_deleted_resources
        assert ("ConfigMap", "chainsaw-storage-tests") in ctrl_deleted_resources

        cell_app = next(app for app in self.apps if app["metadata"]["name"] == "cell-apps")
        cell_components = cell_app["spec"]["generators"][0]["matrix"]["generators"][1]["list"][
            "elements"
        ]
        cell_kh = next(c for c in cell_components if c["component"] == "kuberhealthy")
        cell_rendered = self.render(cell_app, [self.parameters(cell_kh, "minimal")])
        assert cell_rendered.returncode == 0, cell_rendered.stderr
        cell_app_spec = yaml.safe_load(cell_rendered.stdout)
        cell_patches = cell_app_spec["spec"]["sources"][2]["kustomize"]["patches"]
        cell_deleted = {
            p.get("target", {}).get("name")
            for p in cell_patches
            if "$patch: delete" in p.get("patch", "")
        }
        assert "chainsaw-scheduling" not in cell_deleted
        assert "chainsaw-scheduling-tests" not in cell_deleted
        assert "chainsaw-storage" not in cell_deleted
        assert "chainsaw-storage-tests" not in cell_deleted

    def test_opencost_cloud_cost_gated_by_annotation_or_label(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        opencost_comp = next(c for c in components if c["component"] == "opencost")

        # 1. Cloud provider without annotation/label: cloudCost and customPricing disabled
        params_default = self.parameters(opencost_comp, "minimal")
        params_default["metadata"]["labels"]["provider"] = "aws"
        rendered = self.render(app, [params_default])
        assert rendered.returncode == 0, rendered.stderr
        val = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"]["valuesObject"]
        assert "cloudCost" not in val.get("opencost", {})

        # 2. Cloud provider with annotation: enabled
        params_annot = self.parameters(opencost_comp, "minimal")
        params_annot["metadata"]["labels"]["provider"] = "aws"
        params_annot["metadata"]["annotations"]["opencost-cloud-cost-reader"] = "true"
        rendered = self.render(app, [params_annot])
        assert rendered.returncode == 0, rendered.stderr
        val = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"]["valuesObject"]
        assert val["opencost"]["cloudCost"]["enabled"] is True
        assert val["opencost"]["customPricing"]["enabled"] is True

        # 3. Cloud provider with label: enabled
        params_label = self.parameters(opencost_comp, "minimal")
        params_label["metadata"]["labels"]["provider"] = "gcp"
        params_label["metadata"]["labels"]["opencost-cloud-cost-reader"] = "true"
        rendered = self.render(app, [params_label])
        assert rendered.returncode == 0, rendered.stderr
        val = yaml.safe_load(rendered.stdout)["spec"]["sources"][0]["helm"]["valuesObject"]
        assert val["opencost"]["cloudCost"]["enabled"] is True
        assert val["opencost"]["customPricing"]["enabled"] is True

    def test_strict_registry_annotation_on_cloud(self) -> None:
        app = next(app for app in self.apps if app["metadata"]["name"] == "ctrl-apps")
        components = app["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]
        for comp_name in ("coder", "coder-snapshot-portal", "dragonfly-manager"):
            comp = next(c for c in components if c["component"] == comp_name)
            for provider in ("aws", "gcp"):
                with self.subTest(component=comp_name, provider=provider):
                    params = self.parameters(comp, "minimal")
                    params["metadata"]["labels"]["provider"] = provider
                    # Missing registry annotation fails
                    rendered = self.render(app, [params])
                    assert rendered.returncode != 0
                    assert (
                        f"{comp_name} requires ecr-registry or workload-registry annotation on {provider}"
                        in rendered.stderr
                    )

                    # Supplying workload-registry succeeds
                    params["metadata"]["annotations"]["workload-registry"] = "my-workload-registry"
                    rendered = self.render(app, [params])
                    assert rendered.returncode == 0, rendered.stderr


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
