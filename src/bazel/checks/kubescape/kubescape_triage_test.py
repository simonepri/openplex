#!/usr/bin/env python3
"""Test Kubescape scan triage logic for denials, stale exceptions, and negative fixture validation."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from kubescape_triage import main


def make_fixture_files(
    directory: Path,
    policy: dict[str, Any],
    neg_data: dict[str, Any],
    primary_data: dict[str, Any],
) -> tuple[Path, Path, Path]:
    policy_path = directory / "policy.json"
    neg_path = directory / "negative.json"
    primary_path = directory / "primary.json"

    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    neg_path.write_text(json.dumps(neg_data), encoding="utf-8")
    primary_path.write_text(json.dumps(primary_data), encoding="utf-8")

    return policy_path, neg_path, primary_path


class KubescapeTriageTest(unittest.TestCase):
    def test_compliant_scan_exits_zero_without_output(self) -> None:
        policy = {
            "reviewedExceptions": [
                {
                    "controlID": "C-0001",
                    "kind": "Pod",
                    "namespace": "default",
                    "name": "allowed-pod",
                }
            ]
        }
        neg_data = {
            "results": [
                {
                    "controls": [
                        {
                            "controlID": "C-0001",
                            "rules": [{"name": "rule-1", "status": "failed"}],
                        }
                    ]
                }
            ]
        }
        primary_data = {
            "resources": [
                {
                    "resourceID": "res-1",
                    "object": {
                        "kind": "Pod",
                        "metadata": {"name": "allowed-pod", "namespace": "default"},
                    },
                }
            ],
            "results": [
                {
                    "resourceID": "res-1",
                    "controls": [
                        {
                            "controlID": "C-0001",
                            "name": "Host PID",
                            "rules": [{"name": "hostPID", "status": "failed"}],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            policy_p, neg_p, prim_p = make_fixture_files(
                Path(tmp_dir), policy, neg_data, primary_data
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["kubescape_triage.py", str(policy_p), str(neg_p), str(prim_p)])
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")

    def test_negative_fixture_failure_prints_error_and_exits_one(self) -> None:
        policy = {"reviewedExceptions": []}
        neg_data = {"results": []}
        primary_data = {"resources": [], "results": []}

        with tempfile.TemporaryDirectory() as tmp_dir:
            policy_p, neg_p, prim_p = make_fixture_files(
                Path(tmp_dir), policy, neg_data, primary_data
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["kubescape_triage.py", str(policy_p), str(neg_p), str(prim_p)])
            self.assertEqual(exit_code, 1)
            output = stderr.getvalue()
            self.assertIn("intentional security-negative fixture produced no findings", output)

    def test_untriaged_finding_prints_denial_and_exits_one(self) -> None:
        policy = {"reviewedExceptions": []}
        neg_data = {
            "results": [
                {
                    "controls": [
                        {
                            "controlID": "C-0001",
                            "rules": [{"name": "rule-1", "status": "failed"}],
                        }
                    ]
                }
            ]
        }
        primary_data = {
            "resources": [
                {
                    "resourceID": "res-bad",
                    "object": {
                        "kind": "Pod",
                        "metadata": {"name": "unreviewed-pod", "namespace": "prod"},
                    },
                }
            ],
            "results": [
                {
                    "resourceID": "res-bad",
                    "controls": [
                        {
                            "controlID": "C-0099",
                            "name": "Privilege Escalation",
                            "rules": [{"name": "allowPrivilegeEscalation", "status": "failed"}],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            policy_p, neg_p, prim_p = make_fixture_files(
                Path(tmp_dir), policy, neg_data, primary_data
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["kubescape_triage.py", str(policy_p), str(neg_p), str(prim_p)])
            self.assertEqual(exit_code, 1)
            output = stderr.getvalue()
            self.assertIn(
                "untriaged finding: control=C-0099 (Privilege Escalation) kind=Pod name=unreviewed-pod namespace=prod rule=allowPrivilegeEscalation",
                output,
            )

    def test_stale_reviewed_exception_prints_denial_and_exits_one(self) -> None:
        policy = {
            "reviewedExceptions": [
                {
                    "controlID": "C-0002",
                    "kind": "Group",
                    "namespace": "default",
                    "name": "cluster:group:team:stale",
                }
            ]
        }
        neg_data = {
            "results": [
                {
                    "controls": [
                        {
                            "controlID": "C-0001",
                            "rules": [{"name": "rule-1", "status": "failed"}],
                        }
                    ]
                }
            ]
        }
        primary_data = {"resources": [], "results": []}

        with tempfile.TemporaryDirectory() as tmp_dir:
            policy_p, neg_p, prim_p = make_fixture_files(
                Path(tmp_dir), policy, neg_data, primary_data
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["kubescape_triage.py", str(policy_p), str(neg_p), str(prim_p)])
            self.assertEqual(exit_code, 1)
            output = stderr.getvalue()
            self.assertIn(
                "stale reviewed exception: control=C-0002 kind=Group name=cluster:group:team:stale namespace=default never observed",
                output,
            )

    def test_all_findings_and_negative_failure_printed_together(self) -> None:
        policy = {
            "reviewedExceptions": [
                {
                    "controlID": "C-0003",
                    "kind": "Service",
                    "namespace": "default",
                    "name": "unused-service",
                }
            ]
        }
        neg_data = {"results": []}
        primary_data = {
            "resources": [
                {
                    "resourceID": "res-1",
                    "object": {
                        "kind": "Deployment",
                        "metadata": {"name": "app", "namespace": "default"},
                    },
                }
            ],
            "results": [
                {
                    "resourceID": "res-1",
                    "controls": [
                        {
                            "controlID": "C-0050",
                            "name": "Host Network",
                            "rules": [{"name": "hostNetwork", "status": "failed"}],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            policy_p, neg_p, prim_p = make_fixture_files(
                Path(tmp_dir), policy, neg_data, primary_data
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["kubescape_triage.py", str(policy_p), str(neg_p), str(prim_p)])
            self.assertEqual(exit_code, 1)
            output = stderr.getvalue()
            self.assertIn("intentional security-negative fixture produced no findings", output)
            self.assertIn(
                "untriaged finding: control=C-0050 (Host Network) kind=Deployment name=app namespace=default rule=hostNetwork",
                output,
            )
            self.assertIn(
                "stale reviewed exception: control=C-0003 kind=Service name=unused-service namespace=default never observed",
                output,
            )

    def test_insufficient_arguments_returns_exit_code_two(self) -> None:
        exit_code = main(["kubescape_triage.py"])
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
