"""Protects router-only key reuse, rotation and private publication during local bootstrap."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from infra.tools.cloud_emulator.auth import headscale_keys


class HeadscaleKeysTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "src/infra/terraform/deployments/local").mkdir(parents=True)
        (self.root / "src/infra/terraform/deployments/local/deployment.yaml").write_text(
            "clusters:\n  ctrl-test:\n    role: ctrl\n  cell-test:\n    role: cell\n",
            encoding="utf-8",
        )
        self.records: dict[str, dict[str, Any]] = {
            context: {"metadata": {"resourceVersion": "1"}}
            for context in ("ctrl-test", "cell-test")
        }
        self.keys: list[dict[str, Any]] = []
        self.created = self.key(3)
        self.creates = 0
        self.patches: list[str] = []
        self.fail_patch_once = False
        self.missing_external_once = False
        self.targets: dict[str, dict[str, Any]] = {}
        self.refreshes: list[str] = []
        self.enterContext(
            patch.object(
                headscale_keys.compose,
                "configuration",
                return_value=Mock(headscale_container="fixture-headscale"),
            )
        )
        self.enterContext(patch.object(headscale_keys.compose, "run", side_effect=self.command))
        self.enterContext(patch.object(headscale_keys.time, "time", return_value=1_000_000))
        self.enterContext(patch.object(headscale_keys.time, "sleep"))

    @staticmethod
    def key(identifier: int) -> dict[str, Any]:
        return {
            "id": identifier,
            "key": f"hskey-auth-{secrets.token_urlsafe(9)}-{secrets.token_urlsafe(48)}",
            "reusable": True,
            "expiration": {"seconds": 2_000_000},
            "acl_tags": ["tag:k8s-egress", "tag:subnet-router"],
        }

    @staticmethod
    def record(key: dict[str, Any]) -> dict[str, Any]:
        return {
            "metadata": {
                "resourceVersion": "1",
                "annotations": {
                    "headscale-preauth-key-id": str(key["id"]),
                    "headscale-preauth-expiration-epoch": str(key["expiration"]["seconds"]),
                    "headscale-preauth-key-sha256": hashlib.sha256(key["key"].encode()).hexdigest(),
                    "retained-annotation": "preserve",
                },
            },
            "data": {"authkey": base64.b64encode(key["key"].encode()).decode()},
        }

    def command(
        self, arguments: list[str], *, stdin: str | None = None, timeout: int = 20
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if arguments[0] == "docker":
            if "create" in arguments:
                self.creates += 1
                self.create_arguments = arguments
                self.keys.append(self.created)
                output = self.created
            else:
                output = [
                    {**key, "key": f"hskey-auth-{key['key'][11:23]}-***"} for key in self.keys
                ]
        else:
            context = arguments[2]
            if "tailscale-system" in arguments:
                if (
                    "get" in arguments
                    and "externalsecret" in arguments
                    and self.missing_external_once
                ):
                    self.missing_external_once = False
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                if "annotate" in arguments:
                    self.refreshes.append(context)
                    self.targets[context] = copy.deepcopy(self.records[context])
                if "externalsecret" in arguments:
                    return subprocess.CompletedProcess(
                        arguments, 0, "externalsecret/headscale-preauth", ""
                    )
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps(self.targets.get(context, {})), ""
                )
            if "patch" in arguments:
                if self.fail_patch_once:
                    self.fail_patch_once = False
                    raise RuntimeError("private CLI failure")
                document = json.loads(stdin or "")
                if (
                    document["metadata"]["resourceVersion"]
                    != self.records[context]["metadata"]["resourceVersion"]
                ):
                    raise RuntimeError("resource version changed")
                self.records[context]["metadata"].setdefault("annotations", {}).update(
                    document["metadata"]["annotations"]
                )
                self.records[context]["data"] = document["data"]
                self.patches.append(context)
            output = self.records[context]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(output), "")

    def test_existing_tagged_masked_key_is_reused_without_writes(self) -> None:
        self.keys = [self.created]
        self.records = {context: self.record(self.created) for context in self.records}
        self.targets = copy.deepcopy(self.records)
        headscale_keys.reconcile(self.root)
        self.assertEqual(self.creates, 0)
        self.assertEqual(self.patches, [])
        self.assertEqual(self.refreshes, [])

    def test_untagged_key_is_replaced_without_reusing_developer_identity(self) -> None:
        user_key = self.key(1)
        user_key["acl_tags"] = []
        self.keys = [user_key]
        self.records = {context: self.record(user_key) for context in self.records}
        headscale_keys.reconcile(self.root)
        self.assertEqual(self.creates, 1)
        self.assertNotIn("--user", self.create_arguments)
        self.assertIn("tag:k8s-egress,tag:subnet-router", self.create_arguments)
        self.assertEqual(self.patches, ["ctrl-test", "cell-test"])
        self.assertEqual(self.refreshes, ["ctrl-test", "cell-test"])
        for record in self.records.values():
            self.assertEqual(record["metadata"]["annotations"]["headscale-preauth-key-id"], "3")
            self.assertEqual(record["metadata"]["annotations"]["retained-annotation"], "preserve")
        headscale_keys.reconcile(self.root)
        self.assertEqual(self.creates, 1)
        self.assertEqual(len(self.patches), 2)
        self.assertEqual(len(self.refreshes), 2)

    def test_partial_publication_reuses_existing_control_key(self) -> None:
        self.keys = [self.created]
        self.records["ctrl-test"] = self.record(self.created)
        headscale_keys.reconcile(self.root)
        self.assertEqual(self.creates, 0)
        self.assertEqual(self.patches, ["cell-test"])

    def test_stale_targets_refresh_without_rotating_valid_source_keys(self) -> None:
        self.keys = [self.created]
        self.records = {context: self.record(self.created) for context in self.records}
        self.missing_external_once = True
        headscale_keys.reconcile(self.root)
        self.assertEqual(self.creates, 0)
        self.assertEqual(self.patches, [])
        self.assertEqual(self.refreshes, ["ctrl-test", "cell-test"])

    def test_failed_publication_retries_the_same_new_key(self) -> None:
        self.fail_patch_once = True
        headscale_keys.reconcile(self.root)
        self.assertEqual(self.creates, 1)
        self.assertEqual(self.patches, ["ctrl-test", "cell-test"])

    def test_invalid_key_records_cannot_be_reused(self) -> None:
        for field, value in (
            ("reusable", False),
            ("acl_tags", ["tag:subnet-router"]),
            ("acl_tags", ["tag:k8s-egress", "tag:subnet-router", "tag:workspace"]),
            ("expiration", {"seconds": 1_000_000}),
        ):
            with self.subTest(field=field, value=value):
                invalid: dict[str, Any] = {**self.created, field: value}
                masked = {**invalid, "key": f"hskey-auth-{invalid['key'][11:23]}-***"}
                self.assertIsNone(
                    headscale_keys._current_key(self.record(invalid), [masked], 1_086_400)
                )
        record = self.record(self.created)
        record["metadata"]["annotations"]["headscale-preauth-key-sha256"] = "invalid"
        self.assertIsNone(headscale_keys._current_key(record, [self.created], 1_086_400))
        other = self.key(3)
        other["key"] = f"hskey-auth-{other['key'][11:23]}-***"
        self.assertIsNone(
            headscale_keys._current_key(self.record(self.created), [other], 1_086_400)
        )

    def test_invalid_created_key_does_not_publish_and_cli_errors_are_redacted(self) -> None:
        before = copy.deepcopy(self.records)
        self.created["acl_tags"] = []
        with self.assertRaisesRegex(RuntimeError, "invalid router enrollment key"):
            headscale_keys.reconcile(self.root)
        self.assertEqual(self.records, before)
        with patch.object(
            headscale_keys.compose, "run", side_effect=RuntimeError(self.created["key"])
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Local router enrollment command failed"
            ) as failure:
                headscale_keys._run(["docker", "exec"])
        self.assertNotIn(self.created["key"], str(failure.exception))


if __name__ == "__main__":
    unittest.main()
