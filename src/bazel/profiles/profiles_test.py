#!/usr/bin/env python3
"""Test Bazel profile resolution and the profile selections that CI and Coder workspaces make."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar, override

import yaml
from profiles import Bazelrc

# Runfiles mirror the workspace layout, so the workspace files sit three levels up.
WORKSPACE = Path(__file__).parents[3]
PROFILES_RC = WORKSPACE / "src/bazel/profiles/profiles.bazelrc"
TEAM_NAMESPACE = "src/infra/argocd/components/team_namespace/helm"
TEAM_NAMESPACE_CHART = WORKSPACE / "src/infra/argocd/components/team_namespace/chart.tgz"
# team_lane is the only producer of team_namespace values; Argo CD registers
# each cell with the labels of its deployment record.
TEAM_LANE_CHART = WORKSPACE / "src/infra/argocd/components/team_lane/chart.tgz"
DEPLOYMENTS = WORKSPACE / "src/infra/terraform/deployments"
CODER_CONFIG = "workspace-buildbuddy-config"
CLOUD = "grpcs://remote.buildbuddy.io:443"
PROXY_CACHE = "grpc://buildbuddy-enterprise-cache-proxy.buildbuddy.svc.cluster.local:1985"
PROFILE_FLAGS = (
    "bes_backend",
    "bes_results_url",
    "remote_cache",
    "remote_executor",
    "experimental_remote_downloader",
    "extra_execution_platforms",
    "//src/bazel/runners:gpu_runners",
)
_CONFIG_FLAG = re.compile(r"--config=([\w-]+)")
_SHARED_FLAGS = re.compile(r'BAZEL_CONFIG_FLAGS="([^"]*)"')
_COMMAND_BREAK = re.compile(r"\$\(|\)|;|&&|\|\|")

SAMPLE_RC = """
# Comment lines and imports outside a workspace carry no flags.
try-import %workspace%/.bazelrc.user
common --show_progress_rate_limit=5
build --disk_cache=~/cache
build:base --remote_cache=grpc://base
common:base --remote_cache=grpc://common
build:base --keep_going
test:base --remote_cache=grpc://test
build:child --config=base
build:child --remote_cache=grpc://child
build:clear --disk_cache=
build:_hidden --jobs=1
build:loop --config=loop
"""


def _root_rc(*appended: str) -> Bazelrc:
    """Return the root rc with `appended` texts after it, as its `try-import` lines append them."""
    root = (WORKSPACE / ".bazelrc").read_text(encoding="utf-8")
    return Bazelrc(root, *appended, workspace=WORKSPACE)


def _profile_flags(rc: Bazelrc, configs: list[str]) -> dict[str, str]:
    """Return the BuildBuddy flags that a selection sets to a non-empty value."""
    flags = rc.effective_flags(configs)
    return {name: flags[name] for name in PROFILE_FLAGS if flags.get(name)}


def _configs(text: str) -> list[str]:
    return [match.group(1) for match in _CONFIG_FLAG.finditer(text)]


def _script_selections(script: str) -> list[list[str]]:
    """Return the profiles each Bazel invocation in a shell script adds."""
    selections: list[list[str]] = []
    shared: list[str] = []
    for line in script.replace("\\\n", " ").splitlines():
        if exported := _SHARED_FLAGS.search(line):
            shared = _configs(exported.group(1))
            selections.append(shared)
            continue
        for command in _COMMAND_BREAK.split(line):
            configs = _configs(command)
            if "BAZEL_CONFIG_FLAGS" in command:
                configs = shared + configs
            if configs:
                selections.append(configs)
    return selections


def _buildbuddy_selections() -> list[list[str]]:
    workflows = yaml.safe_load((WORKSPACE / "buildbuddy.yaml").read_text(encoding="utf-8"))
    return [
        selection
        for action in workflows["actions"]
        for step in action.get("steps", [])
        for selection in _script_selections(step.get("run", ""))
    ]


class HelmRenderer:
    """Render the team_lane and team_namespace charts the way Argo CD renders them for a deployment."""

    def __init__(self, helm: str, workdir: Path) -> None:
        self._helm = helm
        self._workdir = workdir

    def _render(self, chart: Path, values: dict[str, Any]) -> list[dict[str, Any]]:
        values_path = self._workdir / "values.json"
        values_path.write_text(json.dumps(values), encoding="utf-8")
        result = subprocess.run(
            [self._helm, "template", "render", str(chart), "-f", str(values_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"helm template failed: {result.stderr}")
        return [
            document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)
        ]

    def coder_values(self, deployment: Path) -> dict[str, dict[str, Any]]:
        """Return the team_namespace values of every Coder workspace lane in one deployment."""
        clusters_data = yaml.safe_load(deployment.read_text(encoding="utf-8")).get("clusters", {})
        has_cell = any(record.get("role") == "cell" for record in clusters_data.values())
        if not has_cell:
            return {}
        cluster_registry_entries = []
        registrations = []
        for name, record in clusters_data.items():
            role = record.get("role")
            if role not in {"ctrl", "cell"}:
                continue
            provider = record.get("provider", "aws")
            cloud = record.get("cloud", provider)
            region = record.get("region", "us-west-2")
            if region == "usw2":
                region = "us-west-2"
            aws_region = region
            registrations.append({
                "name": name,
                "server": f"https://{name}.invalid",
                "labels": record.get("labels", {}),
            })
            if role == "ctrl":
                entry: dict[str, Any] = {
                    "name": name,
                    "server": "https://kubernetes.default.svc",
                    "role": "ctrl",
                    "provider": provider,
                    "cloud": cloud,
                    "region": region,
                    "network": {"apiServerCIDRs": ["172.18.0.1/32"]},
                }
                if provider == "floci":
                    entry["localRuntime"] = {
                        "gatewayIPv4": "172.19.255.20",
                        "originRegistry": {
                            "ipv4": "172.19.255.22",
                            "workloadRoot": "origin-registry:5000/000000000000/us-east-1",
                            "workspaceRepository": "origin-registry:5000/000000000000/us-east-1/src/infra/definitions/workspaces/templates/dev",
                            "workspaceSource": "origin-registry:5000/000000000000/us-east-1/src/infra/definitions/workspaces/templates/dev",
                        },
                    }
                cluster_registry_entries.append(entry)
                continue

            entry = {
                "name": name,
                "server": f"https://{name}.example.invalid",
                "role": "cell",
                "provider": provider,
                "cloud": cloud,
                "region": region,
                "network": {"apiServerCIDRs": ["172.17.0.1/32"]},
                "delivery": {
                    "deploymentRepositories": {
                        "src/examples/batch_cron": f"registry.invalid/{name}/src/examples/batch_cron",
                        "src/examples/ray_data": f"registry.invalid/{name}/src/examples/ray_data",
                        "src/examples/ray_serve": f"registry.invalid/{name}/src/examples/ray_serve",
                        "src/examples/ray_train": f"registry.invalid/{name}/src/examples/ray_train",
                        "src/examples/svelte_web": f"registry.invalid/{name}/src/examples/svelte_web",
                    }
                },
                "storage": {
                    "gateway": {
                        "config": {
                            "version": 1,
                            "cells": [
                                {
                                    "name": name,
                                    "provider": provider,
                                    "virtualName": name.replace("cell-", ""),
                                    "crossRegionServer": f"s3-gateway.{name}.example.invalid",
                                }
                            ],
                            "global": {"writerCell": name, "replicaCells": [name]},
                        }
                    },
                    "teams": {
                        "gatewayIdentities": {
                            "examples": {
                                "serviceAccount": "s3-gateway-examples",
                                "roleArn": f"arn:aws:iam::999988887777:role/{name}-gateway",
                            }
                        },
                        "handoffs": {
                            "examples": {
                                "provider": provider,
                                "recordName": "s3-team-examples",
                                "storageContract": {
                                    "version": 1,
                                    "authorizationPrefixes": {
                                        "global": "global/home/examples/",
                                        "global-meta": "global/meta/",
                                        "global-scratch": "global/scratch/examples/",
                                        "home": "home/examples/",
                                        "meta": "meta/",
                                        "scratch": "scratch/examples/",
                                    },
                                    "gateway": {"endpoint": "http://s3-gateway.s3-system.svc"},
                                    "mounts": {
                                        "global": {
                                            "bucket": "global",
                                            "keyPrefix": f"home/examples/{name}/",
                                            "remote": "global",
                                        },
                                        "global-meta": {
                                            "bucket": "global",
                                            "keyPrefix": "meta/",
                                            "remote": "global-meta",
                                        },
                                        "global-scratch": {
                                            "bucket": "global",
                                            "keyPrefix": "scratch/examples/",
                                            "remote": "global-scratch",
                                        },
                                        "home": {
                                            "bucket": name,
                                            "keyPrefix": "home/examples/",
                                            "remote": "home",
                                        },
                                        "meta": {
                                            "bucket": name,
                                            "keyPrefix": "meta/",
                                            "remote": "meta",
                                        },
                                        "scratch": {
                                            "bucket": name,
                                            "keyPrefix": "scratch/examples/",
                                            "remote": "scratch",
                                        },
                                    },
                                },
                                "secretStore": {
                                    "name": "team-s3",
                                    "serviceAccount": "team-s3",
                                    "auth": (
                                        {"aws": {"region": "us-east-1"}}
                                        if provider == "floci"
                                        else {
                                            "aws": {
                                                "region": aws_region,
                                                "roleArn": f"arn:aws:iam::999988887777:role/{name}-record-reader",
                                            }
                                        }
                                    ),
                                },
                                "externalSecret": {
                                    "name": "team-s3",
                                    "targetSecretName": "team-s3",
                                    "remoteProperties": {
                                        "accessKeyId": "access_key_id",
                                        "secretAccessKey": "secret_access_key",
                                    },
                                },
                            }
                        },
                    },
                },
                "cellRecord": {"workspaces": {"enabled": True}},
            }
            if provider != "floci":
                entry["workloadImagePush"] = {
                    "region": aws_region,
                    "network": {
                        "publicHTTPS": True,
                        "privateHttpsCIDRs": (
                            ["10.16.0.10/32", "10.16.64.10/32", "10.16.128.10/32"]
                            if provider == "aws"
                            else []
                        ),
                    },
                    "roles": {
                        "examples": f"arn:aws:iam::999988887777:role/{name}-examples-workspace-ecr"
                    },
                }
                entry["workloadImagePull"] = {
                    "region": aws_region,
                    "roles": {
                        "examples": f"arn:aws:iam::999988887777:role/{name}-examples-ecr-pull"
                    },
                }
                entry["coderProvisioner"] = {
                    "schemaVersion": 1,
                    "incarnation": "a" * 12,
                    "kubeconfig": f"apiVersion: v1\nkind: Config\ncurrent-context: {name}\n",
                    "kubeconfigFile": "/var/run/coder/cell/kubeconfig",
                    "control": {
                        "cluster": f"ctrl-{name.replace('cell-', '')}",
                        "namespace": "coder",
                        "serviceAccount": "coder",
                        "subject": "system:serviceaccount:coder:coder",
                    },
                    "identity": {
                        "roleArn": f"arn:aws:iam::999988887777:role/{name}-coder-provisioner",
                        "kubernetesSubject": {
                            "apiGroup": "rbac.authorization.k8s.io",
                            "kind": "Group",
                            "name": "cluster:coder-provisioners",
                        },
                    },
                    "federation": {
                        "expirationSeconds": 3600,
                        "tokenAudience": "sts.amazonaws.com",
                        "tokenFile": f"/var/run/secrets/coder/aws-federation/{name}/token",
                    },
                    "authentication": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "/opt/coder-provisioner-auth/argocd-k8s-auth",
                        "args": ["aws", "--cluster-name", name],
                    },
                }
            cluster_registry_entries.append(entry)
        if not registrations:
            return {}
        documents = self._render(
            TEAM_LANE_CHART,
            {
                "accessAliasDomain": "c.example.invalid",
                "registeredClusters": registrations,
                "clusterRegistry": {"version": 1, "clusters": cluster_registry_entries},
            },
        )
        lanes: dict[str, dict[str, Any]] = {}
        for document in documents:
            if document.get("kind") != "Application":
                continue
            source = document["spec"]["source"]
            values = source.get("helm", {}).get("valuesObject", {})
            if (
                source.get("path") == TEAM_NAMESPACE
                and values.get("mode", "cell") == "cell"
                and values["devWorkspaces"]["enabled"]
            ):
                lanes[document["metadata"]["name"]] = values
        return lanes

    def coder_rc(self, values: dict[str, Any]) -> str:
        """Return the BuildBuddy rc file that team_namespace writes into Coder workspaces."""
        config = next(
            document
            for document in self._render(TEAM_NAMESPACE_CHART, values)
            if document.get("kind") == "ConfigMap" and document["metadata"]["name"] == CODER_CONFIG
        )
        return str(config["data"]["buildbuddy.bazelrc"])


def _with_buildbuddy(values: dict[str, Any], **buildbuddy: object) -> dict[str, Any]:
    """Return deployment values with the given devWorkspaces.buildbuddy fields replaced."""
    variant = copy.deepcopy(values)
    variant["devWorkspaces"]["buildbuddy"].update(buildbuddy)
    return variant


class BazelrcParsingTest(unittest.TestCase):
    rc = Bazelrc(SAMPLE_RC)

    def test_unconfigured_lines_apply_to_build(self) -> None:
        flags = self.rc.effective_flags([])
        assert flags == {"show_progress_rate_limit": "5", "disk_cache": "~/cache"}

    def test_build_lines_override_common_lines(self) -> None:
        assert self.rc.effective_flags(["base"])["remote_cache"] == "grpc://base"

    def test_test_lines_do_not_apply_to_build(self) -> None:
        assert self.rc.effective_flags(["base"])["remote_cache"] != "grpc://test"

    def test_nested_profiles_expand_in_place(self) -> None:
        flags = self.rc.effective_flags(["child"])
        assert flags["remote_cache"] == "grpc://child"
        assert flags["keep_going"] == "true"

    def test_later_selection_wins(self) -> None:
        assert self.rc.effective_flags(["child", "base"])["remote_cache"] == "grpc://base"

    def test_empty_value_clears_flag(self) -> None:
        assert self.rc.effective_flags(["clear"])["disk_cache"] == ""

    def test_undefined_profile_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "not defined"):
            self.rc.effective_flags(["missing"])

    def test_profile_cycle_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.rc.effective_flags(["loop"])

    def test_profiles_include_every_configured_command(self) -> None:
        assert self.rc.profiles == {"base", "child", "clear", "_hidden", "loop"}

    def test_unconditional_config_lines_form_the_default_selection(self) -> None:
        rc = Bazelrc("common --config=base\nbuild:base --keep_going\nbuild:child --config=base\n")
        assert rc.default_selection == ["base"]
        assert rc.effective_flags([])["keep_going"] == "true"

    def test_base_lines_come_before_appended_text(self) -> None:
        rc = Bazelrc(SAMPLE_RC, "build --config=child\n")
        assert rc.default_selection == ["child"]
        assert rc.effective_flags([])["remote_cache"] == "grpc://child"


class WorkspaceImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "profiles.bazelrc").write_text("build:base --keep_going\n", encoding="utf-8")

    @override
    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_workspace_imports_are_inlined(self) -> None:
        rc = Bazelrc(
            "import %workspace%/profiles.bazelrc\ncommon --config=base\n", workspace=self.root
        )
        assert rc.profiles == {"base"}
        assert rc.effective_flags([])["keep_going"] == "true"

    def test_imports_outside_the_workspace_are_ignored(self) -> None:
        rc = Bazelrc(
            "import /var/run/missing.bazelrc\ntry-import %workspace%/missing\n", workspace=self.root
        )
        assert rc.profiles == frozenset()

    def test_missing_workspace_import_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not exist"):
            Bazelrc("import %workspace%/missing.bazelrc\n", workspace=self.root)

    def test_import_leaving_the_workspace_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "leaves the workspace"):
            Bazelrc("import %workspace%/../escape.bazelrc\n", workspace=self.root)


class SelectionValidationTest(unittest.TestCase):
    rc = _root_rc()

    def test_root_selects_no_default(self) -> None:
        assert self.rc.default_selection == []

    def test_valid_selections_pass(self) -> None:
        for configs in (
            ["ci"],
            ["bb-cloud", "ci"],
            ["bb-cloud", "bb-cloud-proxy", "bb-rbe-sh"],
            ["bb-community"],
            ["bb-cloud", "bb-rbe-cloud"],
        ):
            with self.subTest(configs=configs):
                assert self.rc.validate_selection(configs) == []

    def test_invalid_selections_fail(self) -> None:
        for configs, error in (
            (["bb-cloud", "bb-community"], "at most one flavor"),
            (["bb-cloud-proxy", "bb-community"], "at most one flavor"),
            (["bb-rbe-cloud", "bb-rbe-sh"], "at most one execution profile"),
            (["bb-community", "bb-rbe-sh"], "has no executor"),
            (["_bb-rbe"], "building blocks"),
            (["bb-local"], "not defined"),
        ):
            with self.subTest(configs=configs):
                errors = self.rc.validate_selection(configs)
                assert any(error in e for e in errors), errors

    def test_buildbuddy_workflows_select_valid_profiles(self) -> None:
        selections = _buildbuddy_selections()
        assert selections
        for additions in selections:
            with self.subTest(additions=additions):
                # BuildBuddy workflows select bb-cloud themselves so they never rely on the root default.
                assert additions == ["ci", "bb-cloud"]
                selection = self.rc.default_selection + additions
                assert self.rc.validate_selection(selection) == []


class CoderSelectionTest(unittest.TestCase):
    helm: ClassVar[str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        tools = [Path(value).resolve() for value in " ".join(sys.argv[1:]).split()]
        cls.helm = str(next(path for path in tools if path.name == "helm"))

    @override
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.renderer = HelmRenderer(self.helm, Path(self.tempdir.name))
        self.deployments = {
            f"{path.parent.name}/{lane}": values
            for path in sorted(DEPLOYMENTS.glob("*/deployment.yaml"))
            for lane, values in self.renderer.coder_values(path).items()
        }
        assert self.deployments

    @override
    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _coder_rc(self, values: dict[str, Any]) -> Bazelrc:
        return _root_rc(self.renderer.coder_rc(values))

    def _sample_values(self) -> dict[str, Any]:
        return self.deployments[min(self.deployments)]

    def test_deployed_modes_select_valid_profiles(self) -> None:
        for name, values in sorted(self.deployments.items()):
            with self.subTest(deployment=name, buildbuddy=values["devWorkspaces"]["buildbuddy"]):
                rc = self._coder_rc(values)
                errors = rc.validate_selection(rc.default_selection)
                assert errors == [], f"selection {rc.default_selection}: {errors}"

    def test_community_mode_selects_community_flavor(self) -> None:
        rc = self._coder_rc(_with_buildbuddy(self._sample_values(), mode="community"))
        assert rc.default_selection == ["bb-community"]
        assert rc.validate_selection(rc.default_selection) == []

    def test_cloud_without_executors_caches_on_cloud_and_executes_locally(self) -> None:
        rc = self._coder_rc(_with_buildbuddy(self._sample_values(), mode="cloud"))
        assert rc.validate_selection(rc.default_selection) == []
        flags = _profile_flags(rc, [])
        assert flags["remote_cache"] == CLOUD
        assert "remote_executor" not in flags

    def test_cloud_proxy_caches_through_proxy_and_executes_on_self_hosted_runners(self) -> None:
        values = _with_buildbuddy(
            self._sample_values(), mode="cloud", enterpriseProxy=True, executors="all"
        )
        rc = self._coder_rc(values)
        assert rc.validate_selection(rc.default_selection) == []
        flags = _profile_flags(rc, [])
        assert flags["remote_cache"] == PROXY_CACHE
        assert flags["remote_executor"] == CLOUD
        assert flags["extra_execution_platforms"] == (
            "//src/bazel/runners:sh_cpu,//src/bazel/runners:sh_gpu"
        )
        assert flags["//src/bazel/runners:gpu_runners"] == "true"

    def test_cloud_executors_without_proxy_cache_on_cloud_and_execute_remotely(self) -> None:
        values = _with_buildbuddy(self._sample_values(), mode="cloud", executors="gpu")
        rc = self._coder_rc(values)
        assert rc.validate_selection(rc.default_selection) == []
        flags = _profile_flags(rc, [])
        assert flags["remote_cache"] == CLOUD
        assert flags["remote_executor"] == CLOUD


class ProfileFlagsTest(unittest.TestCase):
    rc = _root_rc()

    def test_ci_alone_contacts_no_buildbuddy_service(self) -> None:
        assert _profile_flags(self.rc, ["ci"]) == {}

    def test_ci_on_cloud_caches_and_downloads_without_executor(self) -> None:
        assert _profile_flags(self.rc, ["ci", "bb-cloud"]) == {
            "bes_backend": CLOUD,
            "bes_results_url": "https://app.buildbuddy.io/invocation/",
            "remote_cache": CLOUD,
            "experimental_remote_downloader": CLOUD,
        }

    def test_ci_without_default_uses_no_buildbuddy(self) -> None:
        profiles = Bazelrc.load(PROFILES_RC, WORKSPACE)
        assert profiles.default_selection == []
        assert _profile_flags(profiles, ["ci"]) == {}


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
