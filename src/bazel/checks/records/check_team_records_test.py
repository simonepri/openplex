#!/usr/bin/env python3
"""Test team schema validation, project ownership consistency, and index generation logic."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, override

if TYPE_CHECKING:
    from collections.abc import Iterator

import yaml
from check_team_records import (
    TeamRecordError,
    check_codeowners,
    check_index,
    codeowned_record_paths,
    derived_namespace,
    generate_codeowners,
    load_record,
    scheduling_index,
    validate_cluster_buildbuddy_capabilities,
    validate_codeowners,
    validate_derived_namespaces,
    validate_dev_secret_schema_contract,
    validate_repository,
    write_codeowners,
    write_index,
)
from jsonschema import Draft7Validator, Draft202012Validator

EXPECTED_VERSION = 3
MAX_KEY_LEN = 127


@contextmanager
def _assert_raises(expected_type: type[BaseException], match: str | None = None) -> Iterator[None]:
    try:
        yield
    except expected_type as err:
        if match is not None and not re.search(match, str(err)):
            msg = f"Expected exception matching {match!r}, got {err!r}"
            raise AssertionError(msg) from err
    else:
        msg = f"Expected {expected_type.__name__} but no exception was raised"
        raise AssertionError(msg)


class BaseTeamRecordTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ray = self.project(
            "src/research/ray_data",
            {"delivery": "submitted"},
        )
        self.svelte = self.project(
            "src/web/svelte_web",
            {
                "delivery": "promoted",
                "stages": [
                    {
                        "name": "test",
                        "promotion": "automatic",
                        "source": {"kind": "warehouse"},
                    },
                    {
                        "name": "prod",
                        "promotion": "manual",
                        "source": {"kind": "stage", "name": "test"},
                    },
                ],
            },
        )
        self.team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "projects": ["svelte_web", "ray_data"],
            },
        )
        schema_path = (
            Path(__file__).resolve().parents[4] / "src/infra/definitions/teams/team.schema.json"
        )
        self.team_schema = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        team_values_schema_path = (
            Path(__file__).resolve().parents[4]
            / "src/infra/argocd/components/team_lane/helm/values.schema.json"
        )
        team_values_schema = json.loads(team_values_schema_path.read_text(encoding="utf-8"))
        self.projected_team_schema = Draft7Validator({
            "$schema": team_values_schema["$schema"],
            "$ref": "#/definitions/team",
            "definitions": team_values_schema["definitions"],
        })
        project_schema_path = Path(__file__).resolve().parent / "project.schema.json"
        self.project_schema = Draft202012Validator(
            json.loads(project_schema_path.read_text(encoding="utf-8"))
        )

    def test_project_schema_accepts_only_workload_records(self) -> None:
        workload = {"delivery": "submitted"}

        assert self.project_schema.is_valid(workload)
        for field, value in {
            "installation": {},
            "name": "cluster_workload",
            "namespace": "workload",
            "release": {"cadence": "on-merge", "bar": ["ci"]},
        }.items():
            with self.subTest(field=field):
                assert not self.project_schema.is_valid({**workload, field: value})

    def test_codeowners_requires_split_infrastructure_authorities(self) -> None:
        local_dep = self.record(
            "src/infra/terraform/deployments/local/deployment.yaml", {"clusters": {}}
        )
        prod_dep = self.record(
            "src/infra/terraform/deployments/research/deployment.yaml", {"clusters": {}}
        )
        codeowners = self.root / ".github/CODEOWNERS"
        codeowners.parent.mkdir(parents=True)
        codeowners.write_text(
            "/src/infra/terraform/deployments/local/deployment.yaml @owner\n"
            "/src/infra/definitions/teams/** @owner\n"
            "/src/research/** @owner\n"
            "/src/web/** @owner\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.root,
            check=True,
        )

        owned_paths = codeowned_record_paths(
            self.root,
            [self.team],
            [self.ray, self.svelte],
        )

        assert local_dep in owned_paths
        assert prod_dep in owned_paths
        with _assert_raises(TeamRecordError, match="research/deployment.yaml"):
            validate_codeowners(self.root, codeowners, owned_paths)

    @override
    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def project(
        self,
        directory: str,
        document: dict[str, Any],
        *,
        manifests: bool = True,
    ) -> Path:
        if manifests and "delivery" not in document:
            document = {"delivery": "submitted", **document}
        path = self.record(f"{directory}/deployment/project.yaml", document)
        if manifests:
            self.write(f"{directory}/deployment/kustomization.yaml", "resources: []\n")
        return path

    def cell(
        self,
        name: str,
        provider: str,
        *,
        workspace_defaults: tuple[int, int] | None = None,
        workspace_storage_min: int = 48,
        ha_quota: dict[str, str] | None = None,
        gpu_classes: dict[str, Any] | None = None,
        tpu_classes: dict[str, Any] | None = None,
        floors: dict[str, Any] | None = None,
        labels: dict[str, Any] | None = None,
    ) -> Path:
        compute: dict[str, Any] = {
            "floors": floors
            or {
                "be": {"cpu": "0", "memory": "0"},
                "ha": ha_quota or {"cpu": "3", "memory": "9Gi"},
                "ma": {"cpu": "0", "memory": "0"},
                "wa": {"cpu": "0", "memory": "0"},
            },
        }
        if gpu_classes is not None:
            compute["gpu_classes"] = gpu_classes
        if tpu_classes is not None:
            compute["tpu_classes"] = tpu_classes
        cell_labels = {"provider": provider, "role": "cell"}
        if labels:
            cell_labels.update(labels)
        document = {
            "name": name,
            "labels": cell_labels,
            "provider": provider,
            "role": "cell",
            "compute": compute,
        }
        if workspace_defaults is not None:
            workspace_cpu_default, workspace_memory_default = workspace_defaults
            document["workspaces"] = {
                "enabled": True,
                "resource_envelope": {
                    "cpu": {"min": 1, "default": workspace_cpu_default},
                    "memory_gib": {"min": 1, "default": workspace_memory_default},
                    "storage_gib": {"min": workspace_storage_min},
                },
            }
        return self.record(f"src/infra/cells/{name}.yaml", document)

    def record(self, relative_path: str, document: dict[str, Any]) -> Path:
        return self.write(relative_path, yaml.safe_dump(document, sort_keys=False))

    @staticmethod
    def write_project(path: Path, document: dict[str, Any]) -> None:
        path.write_text(
            yaml.safe_dump(
                {key: value for key, value in document.items() if key != "_path"},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def write(self, relative_path: str, content: str) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class TeamRecordValidationTest(BaseTeamRecordTest):
    def test_claims_resolve_by_project_directory(self) -> None:
        index = validate_repository(self.root, [self.team], [self.ray, self.svelte])

        assert index["version"] == EXPECTED_VERSION
        assert index["projects"] == [
            {
                "delivery": "submitted",
                "deployments": [{"path": "src/research/ray_data/deployment"}],
                "name": "ray_data",
                "path": "src/research/ray_data",
                "team": "examples",
            },
            {
                "delivery": "promoted",
                "deployments": [
                    {
                        "path": "src/web/svelte_web/deployment",
                        "promotion": "automatic",
                        "source": {"kind": "warehouse"},
                        "stage": "test",
                    },
                    {
                        "path": "src/web/svelte_web/deployment",
                        "promotion": "manual",
                        "source": {"kind": "stage", "name": "test"},
                        "stage": "prod",
                    },
                ],
                "name": "svelte_web",
                "path": "src/web/svelte_web",
                "team": "examples",
            },
        ]

    def test_dev_secret_names_are_unique_snake_case(self) -> None:
        base_record = {
            "slug": "examples",
            "cells": {},
            "quota": {
                "classes": {"ha": {"cpu": "2", "memory": "6Gi"}},
            },
            "projects": ["ray_data"],
            "promotion": "automatic",
            "key_epoch": 1,
        }
        valid_record = {
            **base_record,
            "dev_secrets": ["slack_bot_token", "temporal_cloud_api_key", "a" * 81],
        }
        assert self.team_schema.is_valid(valid_record)

        invalid_lists = [
            ["slack_bot_token", "slack_bot_token"],
            ["SLACK_BOT_TOKEN"],
            ["slack-bot-token"],
            ["slack__bot_token"],
            ["a" * 82],
        ]
        for dev_secrets in invalid_lists:
            with self.subTest(dev_secrets=dev_secrets):
                assert not self.team_schema.is_valid({**base_record, "dev_secrets": dev_secrets})

    def test_team_slug_is_one_injective_encoding_token(self) -> None:
        base_record = {
            "cells": {},
            "quota": {
                "classes": {"ha": {"cpu": "2", "memory": "6Gi"}},
            },
            "projects": ["ray_data"],
            "promotion": "automatic",
            "key_epoch": 1,
        }
        assert self.team_schema.is_valid({**base_record, "slug": "team42"})

        for slug in ["a", "foo-bar", "foo_bar", "42team", "global", "t" * 32]:
            with self.subTest(slug=slug):
                assert not self.team_schema.is_valid({**base_record, "slug": slug})

    def test_schema_contract_rejects_identifier_bound_drift(self) -> None:
        repository_root = Path(__file__).resolve().parents[4]
        schema_paths = [
            "src/infra/definitions/teams/team.schema.json",
            "src/infra/argocd/components/team_lane/helm/values.schema.json",
        ]
        for schema_path in schema_paths:
            self.write(
                schema_path,
                (repository_root / schema_path).read_text(encoding="utf-8"),
            )
        validate_dev_secret_schema_contract(self.root)
        team_lane_schema_path = self.root / schema_paths[-1]
        team_lane_schema = json.loads(team_lane_schema_path.read_text(encoding="utf-8"))
        dev_secrets_field = team_lane_schema["definitions"]["team"]["properties"]["dev_secrets"]
        dev_secrets_field["items"]["maxLength"] = 82
        team_lane_schema_path.write_text(json.dumps(team_lane_schema), encoding="utf-8")

        with _assert_raises(TeamRecordError, match="one dev-secret pattern and maxLength"):
            validate_dev_secret_schema_contract(self.root)

    def test_repository_rejects_ambiguous_team_slug_encodings(self) -> None:
        for slug in ["foo-bar", "foo_bar"]:
            with self.subTest(slug=slug):
                team = self.record(
                    f"src/infra/definitions/teams/{slug}.yaml",
                    {"slug": slug, "projects": ["ray_data", "svelte_web"]},
                )
                with _assert_raises(TeamRecordError, match="lowercase alphanumeric token"):
                    validate_repository(
                        self.root,
                        [team],
                        [self.ray, self.svelte],
                    )

    def test_quota_classes_are_resource_allocations(self) -> None:
        base_record = {
            "slug": "examples",
            "cells": {},
            "projects": ["ray_data"],
            "promotion": "automatic",
            "key_epoch": 1,
            "quota": {
                "classes": {
                    "ha": {
                        "cpu": "2",
                        "memory": "6Gi",
                        "nvidia.com/gpu": "1",
                    }
                },
            },
        }

        per_cell_record = {
            **base_record,
            "quota": {
                **base_record["quota"],
                "cells": {
                    "cell-aws-test": {
                        "classes": base_record["quota"]["classes"],
                    },
                },
            },
        }

        assert self.team_schema.is_valid(base_record)
        assert self.team_schema.is_valid(per_cell_record)
        assert self.projected_team_schema.is_valid({
            key: value
            for key, value in {**per_cell_record, "archive": False}.items()
            if key != "key_epoch"
        })
        assert not self.team_schema.is_valid({**base_record, "quota": {"classes": ["ha"]}})
        assert not self.team_schema.is_valid({
            **base_record,
            "quota": {"classes": {"ha": {"memory": "6Gi"}}},
        })
        assert not self.team_schema.is_valid({
            **base_record,
            "quota": {**base_record["quota"], "weight": 1},
        })

    def test_team_quota_rejects_project_allocations(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "cells": {},
                "projects": ["ray_data"],
                "promotion": "automatic",
                "key_epoch": 1,
                "quota": {
                    "classes": {"ha": {"cpu": "2", "memory": "6Gi"}},
                    "projects": {"ray_data": {"cpu": "1", "memory": "2Gi"}},
                },
            },
        )
        record = load_record(team)

        assert not self.team_schema.is_valid(record)
        with _assert_raises(TeamRecordError, match="quota must contain classes and optional cells"):
            scheduling_index(
                self.root,
                [team],
                [self.cell("cell-aws-test", "aws")],
            )

    def test_scheduling_index_projects_selected_team_quota_per_cell(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "cells": {"matchLabels": {"provider": "aws"}},
                "quota": {
                    "classes": {
                        "be": {"cpu": "0", "memory": "0"},
                        "ha": {"cpu": "2", "memory": "6Gi"},
                    },
                },
            },
        )
        aws = self.cell("cell-aws-test", "aws")
        gcp = self.cell("cell-gcp-test", "gcp")

        index = scheduling_index(self.root, [team], [gcp, aws])

        assert index == {
            "cells": [
                {
                    "name": "cell-aws-test",
                    "teams": [
                        {
                            "quota": {
                                "classes": {
                                    "be": {"cpu": "0", "memory": "0"},
                                    "ha": {"cpu": "2", "memory": "6Gi"},
                                }
                            },
                            "slug": "examples",
                        }
                    ],
                },
                {"name": "cell-gcp-test", "teams": []},
            ],
            "version": 1,
        }

    def test_scheduling_index_resolves_cell_specific_team_quota(self) -> None:
        base_classes = {
            "be": {"cpu": "0", "memory": "0"},
            "ha": {"cpu": "3", "memory": "9Gi"},
            "wa": {"cpu": "0", "memory": "0"},
        }
        aws_classes = {
            **base_classes,
            "ha": {
                "cpu": "16",
                "memory": "64Gi",
                "nvidia.com/gpu": "2",
            },
        }
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "cells": {},
                "quota": {
                    "classes": base_classes,
                    "cells": {"cell-aws-test": {"classes": aws_classes}},
                },
            },
        )
        aws = self.cell(
            "cell-aws-test",
            "aws",
            workspace_defaults=(10, 32),
            ha_quota={
                "cpu": "16",
                "memory": "64Gi",
                "nvidia.com/gpu": "2",
            },
        )
        local = self.cell(
            "cell-eaws-test",
            "floci",
            workspace_defaults=(1, 2),
        )

        index = scheduling_index(self.root, [team], [local, aws])

        projected = {cell["name"]: cell["teams"][0]["quota"]["classes"] for cell in index["cells"]}
        assert projected["cell-aws-test"] == aws_classes
        assert projected["cell-eaws-test"] == base_classes

    def test_cell_quota_overrides_are_selected_and_structurally_complete(self) -> None:
        cases = (
            (
                "unknown",
                {"cell-missing": {"classes": {"ha": {"cpu": "2", "memory": "2Gi"}}}},
                {},
                [self.cell("cell-aws-test", "aws")],
                "references unknown cells: cell-missing",
            ),
            (
                "unselected",
                {"cell-gcp-test": {"classes": {"ha": {"cpu": "2", "memory": "2Gi"}}}},
                {"matchLabels": {"provider": "aws"}},
                [
                    self.cell("cell-aws-test", "aws"),
                    self.cell("cell-gcp-test", "gcp"),
                ],
                "overrides cells not selected by the team: cell-gcp-test",
            ),
            (
                "class mismatch",
                {"cell-aws-test": {"classes": {"ha": {"cpu": "2", "memory": "2Gi"}}}},
                {},
                [self.cell("cell-aws-test", "aws")],
                "must have the same availability classes as quota.classes",
            ),
        )
        for name, overrides, selector, cells, error in cases:
            with self.subTest(name=name):
                team = self.record(
                    "src/infra/definitions/teams/examples.yaml",
                    {
                        "slug": "examples",
                        "cells": selector,
                        "quota": {
                            "classes": {
                                "be": {"cpu": "0", "memory": "0"},
                                "ha": {"cpu": "2", "memory": "2Gi"},
                            },
                            "cells": overrides,
                        },
                    },
                )
                with _assert_raises(TeamRecordError, match=error):
                    scheduling_index(self.root, [team], cells)

    def test_scheduling_index_rejects_team_quota_above_class_floor(self) -> None:
        teams = [
            self.record(
                f"src/infra/definitions/teams/{slug}.yaml",
                {
                    "slug": slug,
                    "cells": {},
                    "quota": {
                        "classes": {"ha": {"cpu": "2", "memory": "6Gi"}},
                    },
                },
            )
            for slug in ("alpha", "bravo")
        ]

        with _assert_raises(TeamRecordError, match="ha cpu quota 4 exceeds class floor 3"):
            scheduling_index(
                self.root,
                teams,
                [self.cell("cell-aws-test", "aws")],
            )

    def test_workspace_enabled_cell_requires_default_team_ha_quota(self) -> None:
        for quota in (
            {"cpu": "1", "memory": "2Gi"},
            {"cpu": "2", "memory": "1Gi"},
            {"cpu": "0", "memory": "0"},
        ):
            with self.subTest(quota=quota):
                team = self.record(
                    "src/infra/definitions/teams/examples.yaml",
                    {
                        "slug": "examples",
                        "cells": {},
                        "quota": {"classes": {"ha": quota}},
                    },
                )
                with _assert_raises(
                    TeamRecordError, match="HA quota must provide at least 2 CPU and 2Gi memory"
                ):
                    scheduling_index(
                        self.root,
                        [team],
                        [self.cell("cell-aws-test", "aws", workspace_defaults=(1, 1))],
                    )

        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "cells": {},
                "quota": {
                    "classes": {"ha": {"cpu": "2", "memory": "2Gi"}},
                },
            },
        )
        scheduling_index(
            self.root,
            [team],
            [self.cell("cell-aws-test", "aws", workspace_defaults=(1, 1))],
        )

    def test_workspace_enabled_cell_requires_build_storage_minimum(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "cells": {},
                "quota": {"classes": {"ha": {"cpu": "2", "memory": "2Gi"}}},
            },
        )

        with _assert_raises(
            TeamRecordError, match="workspace storage minimum must be at least 8Gi"
        ):
            scheduling_index(
                self.root,
                [team],
                [
                    self.cell(
                        "cell-aws-test",
                        "aws",
                        workspace_defaults=(1, 1),
                        workspace_storage_min=7,
                    )
                ],
            )

    def test_scheduling_index_rejects_partial_tpu_slice(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "cells": {},
                "quota": {
                    "classes": {
                        "ma": {
                            "cpu": "1",
                            "google.com/tpu": "1",
                            "memory": "1Gi",
                        }
                    },
                },
            },
        )

        with _assert_raises(TeamRecordError, match="google.com/tpu must be a multiple of four"):
            scheduling_index(
                self.root,
                [team],
                [self.cell("cell-gcp-test", "gcp")],
            )

    def test_quota_classes_support_specific_accelerators(self) -> None:
        accelerator_record = {
            "slug": "examples",
            "cells": {},
            "projects": ["ray_data"],
            "promotion": "automatic",
            "key_epoch": 1,
            "quota": {
                "classes": {
                    "ha": {
                        "cpu": "2",
                        "memory": "6Gi",
                        "accelerators": {
                            "a100-80gb": "2",
                            "v5e-2x2": "1",
                        },
                    },
                    "ma": {
                        "cpu": "4",
                        "memory": "8Gi",
                        "h100": "4",
                    },
                },
            },
        }
        assert self.team_schema.is_valid(accelerator_record)
        assert self.projected_team_schema.is_valid({
            key: value
            for key, value in {**accelerator_record, "archive": False}.items()
            if key != "key_epoch"
        })
        invalid_model_record = {
            **accelerator_record,
            "quota": {
                "classes": {
                    "ha": {
                        "cpu": "2",
                        "memory": "6Gi",
                        "accelerators": {"unsupported-gpu": "1"},
                    },
                },
            },
        }
        assert not self.team_schema.is_valid(invalid_model_record)
        assert not self.projected_team_schema.is_valid({
            key: value
            for key, value in {**invalid_model_record, "archive": False}.items()
            if key != "key_epoch"
        })
        duplicate_acc_record = {
            **accelerator_record,
            "quota": {
                "classes": {
                    "ha": {
                        "cpu": "2",
                        "memory": "6Gi",
                        "a100-80gb": "2",
                        "accelerators": {"a100-80gb": "2"},
                    },
                },
            },
        }
        team_file = self.record("src/infra/definitions/teams/duplicate.yaml", duplicate_acc_record)
        with _assert_raises(
            TeamRecordError, match="duplicate accelerator specification for 'a100-80gb'"
        ):
            scheduling_index(
                self.root,
                [team_file],
                [self.cell("cell-aws-test", "aws")],
            )

    def test_scheduling_index_validates_accelerator_offerings(self) -> None:
        cell = self.cell(
            "cell-test",
            "aws",
            floors={
                "be": {"cpu": "0", "memory": "0"},
                "ha": {"cpu": "4", "memory": "8Gi", "nvidia.com/gpu": "4"},
                "ma": {"cpu": "0", "memory": "0"},
                "wa": {"cpu": "0", "memory": "0"},
            },
            gpu_classes={"l4": {"max_count": 8}},
            tpu_classes={"v5e-2x2": {"max_count": 4}},
        )
        valid_team = self.record(
            "src/infra/definitions/teams/valid.yaml",
            {
                "slug": "valid",
                "cells": {},
                "quota": {
                    "classes": {
                        "ha": {
                            "cpu": "2",
                            "memory": "4Gi",
                            "accelerators": {"l4": "2"},
                        },
                    },
                },
            },
        )
        scheduling_index(self.root, [valid_team], [cell])

        unoffered_gpu_team = self.record(
            "src/infra/definitions/teams/unoffered_gpu.yaml",
            {
                "slug": "unoffered",
                "cells": {},
                "quota": {
                    "classes": {
                        "ha": {
                            "cpu": "2",
                            "memory": "4Gi",
                            "accelerators": {"h100": "1"},
                        },
                    },
                },
            },
        )
        with _assert_raises(
            TeamRecordError,
            match=r"selected team unoffered ha accelerator 'h100' is not offered by cell-test",
        ):
            scheduling_index(self.root, [unoffered_gpu_team], [cell])

        gpu_only_cell = self.cell(
            "cell-gpu-only",
            "aws",
            floors={
                "be": {"cpu": "0", "memory": "0"},
                "ha": {"cpu": "4", "memory": "8Gi", "google.com/tpu": "4"},
                "ma": {"cpu": "0", "memory": "0"},
                "wa": {"cpu": "0", "memory": "0"},
            },
            gpu_classes={"l4": {"max_count": 8}},
            tpu_classes={},
        )
        tpu_team = self.record(
            "src/infra/definitions/teams/tpu_team.yaml",
            {
                "slug": "tputeam",
                "cells": {},
                "quota": {
                    "classes": {
                        "ha": {
                            "cpu": "2",
                            "memory": "4Gi",
                            "accelerators": {"v5e-2x2": "1"},
                        },
                    },
                },
            },
        )
        with _assert_raises(
            TeamRecordError,
            match=r"selected team tputeam ha accelerator 'v5e-2x2' is not offered by cell-gpu-only",
        ):
            scheduling_index(self.root, [tpu_team], [gpu_only_cell])

    def test_scheduling_index_validates_accelerator_floors(self) -> None:
        gpu_cell = self.cell(
            "cell-aws-gpu",
            "aws",
            floors={
                "be": {"cpu": "0", "memory": "0"},
                "ha": {"cpu": "4", "memory": "8Gi", "nvidia.com/gpu": "2"},
                "ma": {"cpu": "0", "memory": "0"},
                "wa": {"cpu": "0", "memory": "0"},
            },
            gpu_classes={"l4": {"max_count": 8}, "a100-80gb": {"max_count": 8}},
        )
        teams_exceed_gpu = [
            self.record(
                f"src/infra/definitions/teams/{slug}.yaml",
                {
                    "slug": slug,
                    "cells": {},
                    "quota": {
                        "classes": {
                            "ha": {
                                "cpu": "1",
                                "memory": "2Gi",
                                "accelerators": {"a100-80gb": "2"},
                            },
                        },
                    },
                },
            )
            for slug in ("team1", "team2")
        ]
        with _assert_raises(
            TeamRecordError, match=r"selected team ha nvidia\.com/gpu quota 4 exceeds class floor 2"
        ):
            scheduling_index(self.root, teams_exceed_gpu, [gpu_cell])

        tpu_cell = self.cell(
            "cell-gcp-tpu",
            "gcp",
            floors={
                "be": {"cpu": "0", "memory": "0"},
                "ha": {"cpu": "0", "memory": "0"},
                "ma": {"cpu": "4", "memory": "8Gi", "google.com/tpu": "4"},
                "wa": {"cpu": "0", "memory": "0"},
            },
            tpu_classes={"v5e-2x2": {"max_count": 4}},
        )
        team_exceed_tpu = self.record(
            "src/infra/definitions/teams/tpu_exceed.yaml",
            {
                "slug": "tpuexceed",
                "cells": {},
                "quota": {
                    "classes": {
                        "ma": {
                            "cpu": "2",
                            "memory": "4Gi",
                            "accelerators": {"v5e-2x2": "2"},
                        },
                    },
                },
            },
        )
        with _assert_raises(
            TeamRecordError, match=r"selected team ma google\.com/tpu quota 8 exceeds class floor 4"
        ):
            scheduling_index(self.root, [team_exceed_tpu], [tpu_cell])


class TeamRecordSchedulingTest(BaseTeamRecordTest):
    def test_derived_namespace_set_is_exact(self) -> None:
        teams = {"examples": load_record(self.team)}
        projects = {
            "ray_data": load_record(self.ray),
            "svelte_web": load_record(self.svelte),
        }

        assert validate_derived_namespaces(teams, projects) == ["team-examples-workloads"]

    def test_duplicate_project_directory_names_are_rejected(self) -> None:
        duplicate = self.project("src/other/ray_data", {"delivery": "submitted"})

        with _assert_raises(TeamRecordError, match="duplicate project directory name"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte, duplicate])

    def test_project_directory_must_be_snake_case(self) -> None:
        invalid = self.project("src/research/ray-data", {"delivery": "submitted"})

        with _assert_raises(TeamRecordError, match="lowercase snake-case source name"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte, invalid])

    def test_project_directory_must_fit_source_name_limit(self) -> None:
        invalid = self.project(f"src/research/{'n' * 64}", {"delivery": "submitted"})

        with _assert_raises(TeamRecordError, match="1-63 character"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte, invalid])

    def test_overlength_derived_namespace_is_rejected(self) -> None:
        with _assert_raises(TeamRecordError, match="does not derive a valid DNS label"):
            derived_namespace(
                "t" * 50, "workloads", self.root / "src/infra/definitions/teams/test.yaml"
            )

    def test_project_cannot_claim_its_own_team(self) -> None:
        svelte = self.project(
            "src/web/spoofed",
            {
                "team": "examples",
            },
        )
        team = self.record(
            "src/infra/definitions/teams/spoofed.yaml",
            {"slug": "spoofed", "projects": ["spoofed"]},
        )

        with _assert_raises(TeamRecordError):
            validate_repository(self.root, [team], [svelte])

    def test_project_cannot_be_claimed_twice(self) -> None:
        other = self.record(
            "src/infra/definitions/teams/other.yaml",
            {"slug": "other", "projects": ["ray_data"]},
        )

        with _assert_raises(TeamRecordError):
            validate_repository(self.root, [self.team, other], [self.ray, self.svelte])

    def test_unclaimed_workload_is_rejected(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {"slug": "examples", "projects": ["svelte_web"]},
        )

        with _assert_raises(TeamRecordError):
            validate_repository(self.root, [team], [self.ray, self.svelte])

    def test_workload_requires_a_delivery_mode(self) -> None:
        document = yaml.safe_load(self.ray.read_text(encoding="utf-8"))
        del document["delivery"]
        self.ray.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

        with _assert_raises(TeamRecordError, match="delivery must be one of"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte])

    def test_promoted_stage_source_must_name_an_earlier_stage(self) -> None:
        document = yaml.safe_load(self.svelte.read_text(encoding="utf-8"))
        document["stages"][0]["source"] = {"kind": "stage", "name": "prod"}
        self.svelte.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

        with _assert_raises(TeamRecordError, match="source must name an earlier stage"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte])

    def test_submitted_project_cannot_declare_stages(self) -> None:
        document = yaml.safe_load(self.ray.read_text(encoding="utf-8"))
        document["stages"] = [
            {
                "name": "test",
                "promotion": "automatic",
                "source": {"kind": "warehouse"},
            }
        ]
        self.ray.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

        with _assert_raises(TeamRecordError, match="submitted project cannot declare stages"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte])

    def test_reserved_team_slug_is_rejected(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/global.yaml",
            {
                "slug": "global",
                "projects": ["ray_data", "svelte_web"],
            },
        )

        with _assert_raises(TeamRecordError):
            validate_repository(self.root, [team], [self.ray, self.svelte])

    def test_reserved_team_slug_legacy_is_rejected(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/legacy.yaml",
            {
                "slug": "legacy",
                "projects": ["ray_data", "svelte_web"],
            },
        )

        with _assert_raises(TeamRecordError):
            validate_repository(self.root, [team], [self.ray, self.svelte])

    def test_team_readers_policy_validation(self) -> None:
        team_all = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "readers": "all",
                "projects": ["ray_data", "svelte_web"],
            },
        )
        validate_repository(self.root, [team_all], [self.ray, self.svelte])

        team_members = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "readers": "members",
                "projects": ["ray_data", "svelte_web"],
            },
        )
        validate_repository(self.root, [team_members], [self.ray, self.svelte])

        team_invalid = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "readers": "public",
                "projects": ["ray_data", "svelte_web"],
            },
        )
        with _assert_raises(TeamRecordError, match="readers must be one of"):
            validate_repository(self.root, [team_invalid], [self.ray, self.svelte])

        base_team_record = {
            "slug": "examples",
            "cells": {"matchLabels": {"role": "cell"}},
            "quota": {"classes": {"ha": {"cpu": "2", "memory": "4Gi"}}},
            "projects": ["project_a"],
            "promotion": "manual",
            "key_epoch": 1,
        }
        assert self.team_schema.is_valid({**base_team_record, "readers": "all"})
        assert self.team_schema.is_valid({**base_team_record, "readers": "members"})
        assert not self.team_schema.is_valid({**base_team_record, "readers": "public"})

    def test_team_submitters_policy_validation(self) -> None:
        team_all = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "submitters": "all",
                "projects": ["ray_data", "svelte_web"],
            },
        )
        validate_repository(self.root, [team_all], [self.ray, self.svelte])

        team_members = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "submitters": "members",
                "projects": ["ray_data", "svelte_web"],
            },
        )
        validate_repository(self.root, [team_members], [self.ray, self.svelte])

        team_invalid = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "submitters": "public",
                "projects": ["ray_data", "svelte_web"],
            },
        )
        with _assert_raises(TeamRecordError, match="submitters must be one of"):
            validate_repository(self.root, [team_invalid], [self.ray, self.svelte])

        base_team_record = {
            "slug": "examples",
            "cells": {"matchLabels": {"role": "cell"}},
            "quota": {"classes": {"ha": {"cpu": "2", "memory": "4Gi"}}},
            "projects": ["project_a"],
            "promotion": "manual",
            "key_epoch": 1,
        }
        assert self.team_schema.is_valid({**base_team_record, "submitters": "all"})
        assert self.team_schema.is_valid({**base_team_record, "submitters": "members"})
        assert not self.team_schema.is_valid({**base_team_record, "submitters": "public"})

    def test_reserved_project_name_is_rejected(self) -> None:
        reserved = self.project(
            "src/workloads/argocd",
            {"delivery": "submitted"},
        )
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "projects": ["ray_data", "svelte_web", "argocd"],
            },
        )

        with _assert_raises(TeamRecordError, match="reserved name"):
            validate_repository(self.root, [team], [self.ray, self.svelte, reserved])

    def test_nested_project_is_rejected(self) -> None:
        nested = self.project(
            "src/research/ray_data/nested",
            {"delivery": "submitted"},
        )
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "slug": "examples",
                "projects": ["ray_data", "svelte_web", "nested"],
            },
        )

        with _assert_raises(TeamRecordError):
            validate_repository(self.root, [team], [self.ray, self.svelte, nested])

    def test_declared_stage_override_requires_a_real_kustomization(self) -> None:
        stage_dir = self.svelte.parent / "prod"
        stage_dir.mkdir(parents=True, exist_ok=True)

        with _assert_raises(TeamRecordError):
            validate_repository(self.root, [self.team], [self.ray, self.svelte])

    def test_claimed_project_requires_a_standalone_deployment(self) -> None:
        (self.ray.parent / "kustomization.yaml").unlink()

        with _assert_raises(TeamRecordError, match="deployment/kustomization.yaml"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte])

    def test_project_root_kustomization_is_rejected(self) -> None:
        self.write("src/research/ray_data/kustomization.yaml", "resources: [deployment]\n")

        with _assert_raises(TeamRecordError, match="project root must not contain"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte])


class TeamRecordRepositoryTest(BaseTeamRecordTest):
    def test_project_root_project_yaml_is_rejected(self) -> None:
        self.write("src/research/ray_data/project.yaml", "delivery: submitted\n")

        with _assert_raises(TeamRecordError, match="project root must not contain"):
            validate_repository(self.root, [self.team], [self.ray, self.svelte])

    def test_project_yaml_outside_deployment_is_rejected(self) -> None:
        outside = self.record("src/research/ray_data/project.yaml", {"delivery": "submitted"})

        with _assert_raises(TeamRecordError, match="must live in a deployment/ directory"):
            validate_repository(self.root, [self.team], [outside, self.svelte])

    def test_checked_index_detects_stale_generated_state(self) -> None:
        index = validate_repository(self.root, [self.team], [self.ray, self.svelte])
        output = self.root / "project-index.json"
        write_index(output, index)
        check_index(output, index)
        output.write_text("{}\n", encoding="utf-8")

        with _assert_raises(TeamRecordError):
            check_index(output, index)

    def test_generate_codeowners_compiles_nested_files(self) -> None:
        header = (
            "# Generated file; do not edit.\n"
            "# Source: nested CODEOWNERS files throughout the repository.\n\n"
        )
        assert generate_codeowners(self.root) == header

        self.write("src/research/CODEOWNERS", "* @team-research\nmodels/ @team-nlp\n# comment\n")
        self.write("src/web/CODEOWNERS", "\n\n* @team-web\n")
        compiled = generate_codeowners(self.root)
        expected = (
            "# Generated file; do not edit.\n"
            "# Source: nested CODEOWNERS files throughout the repository.\n"
            "\n"
            "# src/research/CODEOWNERS\n"
            "/src/research/* @team-research\n"
            "/src/research/models/ @team-nlp\n"
            "\n"
            "# src/web/CODEOWNERS\n"
            "/src/web/* @team-web\n\n"
        )
        assert compiled == expected

    def test_check_codeowners_detects_stale_content(self) -> None:
        codeowners = self.root / ".github/CODEOWNERS"
        content = generate_codeowners(self.root)
        write_codeowners(codeowners, content)
        check_codeowners(codeowners, content)
        codeowners.write_text("# manual edit\n", encoding="utf-8")
        with _assert_raises(TeamRecordError, match="stale"):
            check_codeowners(codeowners, content)

    @staticmethod
    def test_cluster_buildbuddy_defaults_are_valid() -> None:
        clusters = {
            "cluster-a": {"labels": {}},
            "cluster-b": {},
        }
        validate_cluster_buildbuddy_capabilities(clusters)

    @staticmethod
    def test_cluster_buildbuddy_community_mode_valid() -> None:
        clusters = {
            "cluster-a": {
                "labels": {
                    "buildbuddy.io/enterprise-proxy": "disabled",
                    "buildbuddy.io/executors": "none",
                    "buildbuddy.io/mode": "community",
                }
            }
        }
        validate_cluster_buildbuddy_capabilities(clusters)

    def test_cluster_buildbuddy_cloud_mode_valid_configurations(self) -> None:
        for executors in ("all", "cpu", "gpu", "none"):
            with self.subTest(executors=executors):
                clusters = {
                    "cluster-a": {
                        "labels": {
                            "buildbuddy.io/enterprise-proxy": "enabled",
                            "buildbuddy.io/executors": executors,
                            "buildbuddy.io/mode": "cloud",
                        }
                    }
                }
                validate_cluster_buildbuddy_capabilities(clusters)

    @staticmethod
    def test_cluster_buildbuddy_rejects_invalid_mode() -> None:
        clusters = {
            "cluster-a": {
                "labels": {
                    "buildbuddy.io/mode": "enterprise",
                }
            }
        }
        with _assert_raises(
            TeamRecordError, match="invalid mode 'enterprise' for 'buildbuddy.io/mode'"
        ):
            validate_cluster_buildbuddy_capabilities(clusters)

    @staticmethod
    def test_cluster_buildbuddy_rejects_invalid_proxy() -> None:
        clusters = {
            "cluster-a": {
                "labels": {
                    "buildbuddy.io/enterprise-proxy": "auto",
                    "buildbuddy.io/mode": "cloud",
                }
            }
        }
        with _assert_raises(
            TeamRecordError, match="invalid proxy value 'auto' for 'buildbuddy.io/enterprise-proxy'"
        ):
            validate_cluster_buildbuddy_capabilities(clusters)

    @staticmethod
    def test_cluster_buildbuddy_community_mode_rejects_enterprise_proxy() -> None:
        clusters = {
            "cluster-a": {
                "labels": {
                    "buildbuddy.io/enterprise-proxy": "enabled",
                    "buildbuddy.io/mode": "community",
                }
            }
        }
        with _assert_raises(
            TeamRecordError,
            match=r"'buildbuddy\.io/enterprise-proxy: enabled' is invalid when 'buildbuddy\.io/mode' is 'community'",
        ):
            validate_cluster_buildbuddy_capabilities(clusters)

    def test_cluster_buildbuddy_community_mode_rejects_self_hosted_executors(self) -> None:
        for executors in ("all", "cpu", "gpu"):
            with self.subTest(executors=executors):
                clusters = {
                    "cluster-a": {
                        "labels": {
                            "buildbuddy.io/executors": executors,
                            "buildbuddy.io/mode": "community",
                        }
                    }
                }
                with _assert_raises(
                    TeamRecordError, match="self-hosted executors require Cloud mode"
                ):
                    validate_cluster_buildbuddy_capabilities(clusters)

    @staticmethod
    def test_cluster_buildbuddy_cloud_mode_rejects_invalid_executors() -> None:
        clusters = {
            "cluster-a": {
                "labels": {
                    "buildbuddy.io/executors": "distributed",
                    "buildbuddy.io/mode": "cloud",
                }
            }
        }
        with _assert_raises(
            TeamRecordError,
            match="invalid executors value 'distributed' for 'buildbuddy.io/executors'",
        ):
            validate_cluster_buildbuddy_capabilities(clusters)

    def test_scheduling_index_rejects_invalid_cluster_buildbuddy_capabilities(self) -> None:
        team = self.record(
            "src/infra/definitions/teams/examples.yaml",
            {
                "cells": {"matchLabels": {"provider": "aws"}},
                "quota": {
                    "classes": {
                        "be": {"cpu": "0", "memory": "0"},
                        "ha": {"cpu": "2", "memory": "6Gi"},
                    },
                },
                "slug": "examples",
            },
        )
        invalid_cell = self.cell(
            "cell-aws-test",
            "aws",
            labels={
                "buildbuddy.io/enterprise-proxy": "enabled",
                "buildbuddy.io/mode": "community",
            },
        )
        with _assert_raises(
            TeamRecordError,
            match=r"'buildbuddy\.io/enterprise-proxy: enabled' is invalid when 'buildbuddy\.io/mode' is 'community'",
        ):
            scheduling_index(self.root, [team], [invalid_cell])


class TeamLaneChartTest(unittest.TestCase):
    helm: ClassVar[str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        tools = [Path(value).resolve() for value in " ".join(sys.argv[1:]).split()]
        cls.helm = str(next(path for path in tools if path.name == "helm"))

    @override
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[4]
        self.chart = self.root / "src/infra/argocd/components/team_namespace/helm"
        self.team_chart = self.root / "src/infra/argocd/components/team_lane/helm"
        self.values = yaml.safe_load((self.chart / "lint-values.yaml").read_text(encoding="utf-8"))
        self.team_schema = Draft202012Validator(
            json.loads(
                (self.root / "src/infra/definitions/teams/team.schema.json").read_text(
                    encoding="utf-8"
                )
            )
        )

    @override
    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_local_queues_route_projects_without_project_quota(self) -> None:
        documents = self.render_values(self.values, "local-queues")
        queues = {
            document["metadata"]["name"]: document
            for document in documents
            if document.get("kind") == "LocalQueue"
        }

        assert set(queues) == {"ha", "wa"}
        assert queues["ha"]["spec"] == {"clusterQueue": "team-fixtureteam-ha"}
        assert queues["wa"]["spec"] == {"clusterQueue": "team-fixtureteam-wa"}

    def test_team_foundations_satisfy_schema(self) -> None:
        rendered = subprocess.run(
            [
                self.helm,
                "template",
                "fleet-team",
                str(self.team_chart),
                "-f",
                str(self.team_chart / "lint-values.yaml"),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        foundation_values_by_provider: dict[str, dict[str, Any]] = {}
        for document in yaml.safe_load_all(rendered):
            if not isinstance(document, dict) or document.get("kind") != "Application":
                continue
            source = document.get("spec", {}).get("source", {})
            values = source.get("helm", {}).get("valuesObject", {})
            if (
                source.get("path") != "src/infra/argocd/components/team_namespace/helm"
                or values.get("mode", "cell") != "cell"
            ):
                continue
            handoff = values.get("storageProjection", {}).get("handoff", {})
            provider = handoff.get("provider")
            if provider and provider not in foundation_values_by_provider:
                foundation_values_by_provider[provider] = values

        assert set(foundation_values_by_provider) == {"aws", "floci", "gcp"}
        for provider, values in foundation_values_by_provider.items():
            with self.subTest(provider=provider):
                documents = self.render_values(values, f"team-foundation-{provider}")
                assert documents

    def test_local_storage_rbac_has_one_owner_and_exact_namespace_bindings(self) -> None:
        team_render = subprocess.run(
            [
                self.helm,
                "template",
                "fleet-team",
                str(self.team_chart),
                "-f",
                str(self.team_chart / "lint-values.yaml"),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        team_documents = [
            document for document in yaml.safe_load_all(team_render) if isinstance(document, dict)
        ]
        foundations = [
            document
            for document in team_documents
            if document.get("kind") == "Application"
            and document.get("spec", {}).get("source", {}).get("path")
            == "src/infra/argocd/components/team_namespace/helm"
            and document
            .get("spec", {})
            .get("source", {})
            .get("helm", {})
            .get("valuesObject", {})
            .get("mode", "cell")
            == "cell"
        ]
        rbac_applications = [
            document
            for document in team_documents
            if document.get("kind") == "Application"
            and document
            .get("spec", {})
            .get("source", {})
            .get("helm", {})
            .get("valuesObject", {})
            .get("mode")
            == "storage-rbac"
        ]
        team_values = yaml.safe_load(
            (self.team_chart / "lint-values.yaml").read_text(encoding="utf-8")
        )
        team_slug = team_values["team"]["slug"]
        expected_cells = {
            cluster["name"]
            for cluster in team_values["registeredClusters"]
            if cluster["labels"]["role"] == "cell"
        }
        expected_suffixes = ["workloads"]
        local_cells = {
            cluster["name"]
            for cluster in team_values["registeredClusters"]
            if cluster["labels"]["role"] == "cell" and cluster["labels"]["provider"] == "floci"
        }

        assert len(foundations) == len(expected_cells) * len(expected_suffixes)
        assert {
            (
                application["spec"]["destination"]["name"],
                application["spec"]["source"]["helm"]["valuesObject"]["team"]["namespaceSuffix"],
            )
            for application in foundations
        } == {(cell, suffix) for cell in expected_cells for suffix in expected_suffixes}
        assert {application["metadata"]["name"] for application in rbac_applications} == {
            f"{cell}-team-{team_slug}-storage-record-rbac" for cell in local_cells
        }
        assert {
            application["spec"]["destination"]["name"] for application in rbac_applications
        } == local_cells
        for application in rbac_applications:
            assert (
                application["spec"]["source"]["helm"]["valuesObject"]["storageProjection"][
                    "rbacNamespaceSuffixes"
                ]
                == expected_suffixes
            )

        foundation_documents = self.render_values(self.values, "cell-foundation")
        assert not any(
            document.get("kind") in {"Role", "RoleBinding"}
            and document.get("metadata", {}).get("namespace") == "secret-records"
            for document in foundation_documents
        )

        rbac_values = rbac_applications[0]["spec"]["source"]["helm"]["valuesObject"]
        rbac_documents = self.render_values(rbac_values, "storage-rbac")
        roles = [document for document in rbac_documents if document.get("kind") == "Role"]
        bindings = [
            document for document in rbac_documents if document.get("kind") == "RoleBinding"
        ]

        assert len(roles) == 1
        assert roles[0]["metadata"] == {
            "labels": {
                "app.kubernetes.io/managed-by": "argocd",
                "app.kubernetes.io/part-of": "team-lane",
            },
            "name": f"s3-team-{team_slug}-reader",
            "namespace": "secret-records",
        }
        assert roles[0]["rules"] == [
            {
                "apiGroups": [""],
                "resourceNames": [f"s3-team-{team_slug}"],
                "resources": ["secrets"],
                "verbs": ["get"],
            },
            {
                "apiGroups": ["authorization.k8s.io"],
                "resources": ["selfsubjectrulesreviews"],
                "verbs": ["create"],
            },
        ]
        assert len(bindings) == 1
        assert bindings[0]["metadata"]["name"] == f"s3-team-{team_slug}-reader"
        assert {subject["namespace"] for subject in bindings[0]["subjects"]} == {
            f"team-{team_slug}-{suffix.replace('_', '-')}" for suffix in expected_suffixes
        }
        assert all(
            subject["kind"] == "ServiceAccount" and subject["name"] == "team-s3"
            for subject in bindings[0]["subjects"]
        )
        assert bindings[0]["roleRef"] == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": f"s3-team-{team_slug}-reader",
        }

    def test_provider_identifier_length_boundary(self) -> None:
        slug = "t" * 31
        valid_record = {
            "cells": {},
            "dev_secrets": ["a" * 81],
            "key_epoch": 1,
            "projects": ["ray_data"],
            "promotion": "automatic",
            "quota": {"classes": {"ha": {"cpu": "2", "memory": "6Gi"}}},
            "slug": slug,
        }
        assert self.team_schema.is_valid(valid_record)
        assert len(f"cluster-teams-{slug}-{'a' * 81}") == MAX_KEY_LEN

        invalid_secret_record = {**valid_record, "dev_secrets": ["a" * 82]}
        assert not self.team_schema.is_valid(invalid_secret_record)

        invalid_slug_record = {**valid_record, "slug": "t" * 32}
        assert not self.team_schema.is_valid(invalid_slug_record)

    def render_values(self, values: dict[str, Any], name: str) -> list[dict[str, Any]]:
        values_path = Path(self.tempdir.name) / f"{name}.yaml"
        values_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        rendered = subprocess.run(
            [self.helm, "template", "team-lane", str(self.chart), "-f", str(values_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return [document for document in yaml.safe_load_all(rendered) if isinstance(document, dict)]


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
