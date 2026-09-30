#!/usr/bin/env python3
"""Protects storage-sync Job identity, admission, dry-run, and team-boundary behavior."""

import argparse
import unittest
from typing import override

from src.infra.tools.storage_sync.manifest import job_manifest
from src.infra.tools.storage_sync.topology import StorageTopology


def options(**overrides: object) -> argparse.Namespace:
    values = {
        "source": "s3://aws-usw2/home/examples/source",
        "target": "s3://gcp-euw4/home/examples/target",
        "availability": "ma",
        "latency": "lt",
        "approve": False,
        "requester": "developer@example.com",
        "team": "examples",
        "namespace": "team-examples-workloads",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class StorageTopologyTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.topology = StorageTopology.from_dict({
            "team": "examples",
            "cells": "aws-usw2 s3-aws-usw2\ngcp-euw4 s3-gcp-euw4\nglobal s3-global",
            "global-writer-cell": "aws-usw2",
            "global-cells": "aws-usw2\ngcp-euw4",
            "egress-usd-per-gib": "0.02",
        })

    def test_resolves_valid_cell_uri(self) -> None:
        res = self.topology.resolve("s3://aws-usw2/home/examples/data/model.bin")
        assert res.top == "aws-usw2"
        assert res.cell == "aws-usw2"
        assert res.remote_path == "s3-aws-usw2:home/examples/data/model.bin"
        assert not res.is_global

    def test_resolves_global_uri(self) -> None:
        res = self.topology.resolve("s3://global/home/examples/shared/dataset")
        assert res.top == "global"
        assert res.cell == "aws-usw2"
        assert res.remote_path == "s3-global:home/examples/shared/dataset"
        assert res.is_global

    def test_rejects_other_team(self) -> None:
        try:
            self.topology.resolve("s3://aws-usw2/home/other-team/data")
        except ValueError:
            pass
        else:
            msg = "Expected ValueError"
            raise AssertionError(msg)

    def test_rejects_global_scratch(self) -> None:
        try:
            self.topology.resolve("s3://global/scratch/examples/temp")
        except ValueError:
            pass
        else:
            msg = "Expected ValueError"
            raise AssertionError(msg)


class JobManifestTest(unittest.TestCase):
    @staticmethod
    def test_dry_run_uses_distinct_identity_and_public_scheduling_intent() -> None:
        manifest = job_manifest(options(), "storage-sync-test", "2026-09-05T00:00:00Z")
        pod = manifest["spec"]["template"]["spec"]
        env = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
        assert pod["serviceAccountName"] == "storage-sync"
        assert not pod["automountServiceAccountToken"]
        assert "priorityClassName" not in pod
        assert "kueue.x-k8s.io/queue-name" not in manifest["metadata"]["labels"]
        assert manifest["metadata"]["labels"]["availability-class"] == "ma"
        assert manifest["metadata"]["labels"]["latency-class"] == "lt"
        assert env["SYNC_APPROVED"] == "false"
        assert [
            volume["secret"]["secretName"] for volume in pod["volumes"] if "secret" in volume
        ] == ["team-s3", "team-gcs"]
        assert env["AWS_PROFILE"] == "team-s3"
        assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/var/run/cluster/s3/credentials"
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/var/run/cluster/gcs/credentials.json"

    @staticmethod
    def test_aws_only_credentials() -> None:
        manifest = job_manifest(
            options(target="s3://aws-usw2/scratch/examples/target"),
            "storage-sync-test",
            "2026-09-05T00:00:00Z",
        )
        pod = manifest["spec"]["template"]["spec"]
        secrets = [v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v]
        assert secrets == ["team-s3"]
        env = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
        assert env["AWS_PROFILE"] == "team-s3"
        assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/var/run/cluster/s3/credentials"
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env

    @staticmethod
    def test_gcp_only_credentials() -> None:
        manifest = job_manifest(
            options(
                source="s3://gcp-euw4/home/examples/source",
                target="s3://gcp-euw4/scratch/examples/target",
            ),
            "storage-sync-test",
            "2026-09-05T00:00:00Z",
        )
        pod = manifest["spec"]["template"]["spec"]
        secrets = [v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v]
        assert secrets == ["team-gcs"]
        env = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
        assert "AWS_PROFILE" not in env
        assert "AWS_SHARED_CREDENTIALS_FILE" not in env
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/var/run/cluster/gcs/credentials.json"

    @staticmethod
    def test_approve_is_explicit() -> None:
        manifest = job_manifest(options(approve=True), "storage-sync-test", "2026-09-05T00:00:00Z")
        env = {
            item["name"]: item["value"]
            for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["SYNC_APPROVED"] == "true"

    def test_rejects_other_team_and_global_scratch(self) -> None:
        for target in (
            "s3://gcp-euw4/home/other/data",
            "s3://global/scratch/examples/data",
        ):
            with self.subTest(target=target):
                try:
                    job_manifest(
                        options(target=target),
                        "storage-sync-test",
                        "2026-09-05T00:00:00Z",
                    )
                except ValueError:
                    pass
                else:
                    msg = "Expected ValueError"
                    raise AssertionError(msg)

    @staticmethod
    def test_command_arguments_safely_parameterized() -> None:
        topology = StorageTopology.from_dict({
            "team": "examples",
            "cells": "aws-usw2 s3-aws-usw2\ngcp-euw4 s3-gcp-euw4",
            "global-writer-cell": "aws-usw2",
            "global-cells": "aws-usw2\ngcp-euw4",
        })
        manifest_dry = job_manifest(
            options(source="s3://aws-usw2/home/examples/model$(echo injected).bin"),
            "storage-sync-dry",
            "2026-09-05T00:00:00Z",
            topology=topology,
        )
        dry_cmd = manifest_dry["spec"]["template"]["spec"]["containers"][0]["command"]
        assert dry_cmd[0] == "sh"
        assert dry_cmd[1] == "-c"
        assert "$(echo injected)" not in dry_cmd[2]
        assert dry_cmd[3] == "storage-sync"
        assert dry_cmd[4] == "s3-aws-usw2:home/examples/model$(echo injected).bin"
