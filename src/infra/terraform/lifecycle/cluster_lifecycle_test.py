#!/usr/bin/env python3
"""Tests cluster lifecycle workflows including OpenTofu plan/apply invocations and teardown sequences."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from infra.terraform.lifecycle import (
    cluster_common,
    cluster_down,
    cluster_up,
)
from infra.tools.cloud_emulator.access import cluster_tailnet


class BaseLifecycleTestCase(unittest.TestCase):
    """Base test case suppressing noisy workflow stdout output."""

    def setUp(self) -> None:
        super().setUp()
        self._stdout_patcher = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.mock_stdout = self._stdout_patcher.start()

    def tearDown(self) -> None:
        self._stdout_patcher.stop()
        super().tearDown()


class TestArgumentParsing(BaseLifecycleTestCase):
    """Test CLI argument parsing for cluster_up and cluster_down."""

    def test_cluster_up_default_args(self) -> None:
        args = cluster_up.parse_args([])
        self.assertEqual(args.target, "local")
        self.assertEqual(args.availability, "standalone")
        self.assertEqual(args.timeout, 600)
        self.assertFalse(args.skip_wait)
        self.assertIsNone(args.context)

    def test_cluster_up_custom_args(self) -> None:
        args = cluster_up.parse_args([
            "--target",
            "production",
            "--availability",
            "resilient",
            "--timeout",
            "120",
            "--skip-wait",
            "--context",
            "prod-cluster-ctx",
        ])
        self.assertEqual(args.target, "production")
        self.assertEqual(args.availability, "resilient")
        self.assertEqual(args.timeout, 120)
        self.assertTrue(args.skip_wait)
        self.assertEqual(args.context, "prod-cluster-ctx")

    def test_cluster_up_invalid_availability(self) -> None:
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                cluster_up.parse_args(["--availability", "nonexistent"])

    def test_cluster_down_default_args(self) -> None:
        args = cluster_down.parse_args([])
        self.assertEqual(args.target, "local")
        self.assertEqual(args.timeout, 300)
        self.assertFalse(args.destroy)

    def test_cluster_down_custom_args(self) -> None:
        args = cluster_down.parse_args(["--target", "production", "--timeout", "45"])
        self.assertEqual(args.target, "production")
        self.assertEqual(args.timeout, 45)

    def test_cluster_down_explicit_destroy(self) -> None:
        args = cluster_down.parse_args(["--destroy"])
        self.assertEqual(args.target, "local")
        self.assertTrue(args.destroy)


class TestPreflightCheck(BaseLifecycleTestCase):
    """Test local Docker and Colima preflight validation."""

    @mock.patch.object(cluster_up.runtime, "ensure_docker_local")
    def test_preflight_skipped_for_non_local(self, mock_ensure_docker: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / ".terraform").mkdir()
            with (
                mock.patch.object(cluster_up, "ensure_deployment_dir", return_value=temp_path),
                mock.patch.object(cluster_up, "run_tofu_init") as mock_init,
                mock.patch.object(cluster_up, "run_tofu_apply"),
                mock.patch.object(cluster_up.runtime, "start") as mock_start,
                mock.patch.object(cluster_up.runtime, "reconcile_registry") as mock_registry,
                mock.patch.object(cluster_up.kubeconfig, "project") as mock_project,
                mock.patch.object(cluster_up, "seed_local_images") as mock_seed,
                mock.patch.object(cluster_up.runtime, "reconcile_tls") as mock_tls,
                mock.patch.object(cluster_up.headscale_keys, "reconcile") as mock_keys,
            ):
                cluster_up.cluster_up(
                    cluster_up.ClusterUpConfig(target="production", skip_wait=True)
                )
                mock_ensure_docker.assert_not_called()
                mock_start.assert_not_called()
                mock_registry.assert_not_called()
                mock_project.assert_not_called()
                mock_seed.assert_not_called()
                mock_tls.assert_not_called()
                mock_keys.assert_not_called()
                mock_init.assert_called_once_with(temp_path)


class TestTofuExecution(BaseLifecycleTestCase):
    """Test OpenTofu init and apply executions."""

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_tofu_init_creates_configured_plugin_cache(self, mock_run: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_cache = root / "cache" / "plugins"
            with mock.patch.dict(
                os.environ,
                {"TF_PLUGIN_CACHE_DIR": str(plugin_cache)},
            ):
                cluster_common.run_tofu_init(root / "production")

            self.assertTrue(plugin_cache.is_dir())
            mock_run.assert_called_once_with(
                ["tofu", f"-chdir={root / 'production'}", "init"],
                check=True,
                env=None,
            )

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_tofu_init_called_when_terraform_dir_missing(self, mock_run: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            with (
                mock.patch.object(cluster_up, "ensure_deployment_dir", return_value=temp_path),
                mock.patch.object(cluster_up.runtime, "ensure_docker_local"),
                mock.patch.object(cluster_up.runtime, "start"),
                mock.patch.object(cluster_up.runtime, "reconcile_registry"),
                mock.patch.object(cluster_up.kubeconfig, "project"),
                mock.patch.object(cluster_up, "seed_local_images"),
                mock.patch.object(cluster_up.runtime, "reconcile_tls"),
                mock.patch.object(cluster_up.headscale_keys, "reconcile"),
            ):
                cluster_up.cluster_up(
                    cluster_up.ClusterUpConfig(
                        target="local",
                        availability="standalone",
                        skip_wait=True,
                        with_tailnet=False,
                    )
                )

            expected_init = ["tofu", f"-chdir={temp_path}", "init"]
            expected_apply = [
                "tofu",
                f"-chdir={temp_path}",
                "apply",
                "-auto-approve",
                "-var=fleet_availability=standalone",
            ]
            self.assertEqual(mock_run.call_count, 2)
            mock_run.assert_any_call(expected_init, check=True, env=None)
            mock_run.assert_any_call(expected_apply, check=True, env=None)

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_tofu_init_refreshes_when_terraform_dir_exists(self, mock_run: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / ".terraform").mkdir()
            with (
                mock.patch.object(cluster_up, "ensure_deployment_dir", return_value=temp_path),
                mock.patch.object(cluster_up.runtime, "ensure_docker_local"),
                mock.patch.object(cluster_up.runtime, "start"),
                mock.patch.object(cluster_up.runtime, "reconcile_registry"),
                mock.patch.object(cluster_up.kubeconfig, "project"),
                mock.patch.object(cluster_up, "seed_local_images"),
                mock.patch.object(cluster_up.runtime, "reconcile_tls"),
                mock.patch.object(cluster_up.headscale_keys, "reconcile"),
            ):
                cluster_up.cluster_up(
                    cluster_up.ClusterUpConfig(
                        target="local",
                        availability="replicated",
                        skip_wait=True,
                        with_tailnet=False,
                    )
                )

            expected_init = ["tofu", f"-chdir={temp_path}", "init"]
            expected_apply = [
                "tofu",
                f"-chdir={temp_path}",
                "apply",
                "-auto-approve",
                "-var=fleet_availability=replicated",
            ]
            self.assertEqual(mock_run.call_count, 2)
            mock_run.assert_any_call(expected_init, check=True, env=None)
            mock_run.assert_any_call(expected_apply, check=True, env=None)

    def test_missing_deployment_dir_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            cluster_common.ensure_deployment_dir("nonexistent_deployment_xyz")

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_tofu_apply_failure_propagates(self, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, ["tofu", "apply"])
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / ".terraform").mkdir()
            with self.assertRaises(subprocess.CalledProcessError):
                cluster_common.run_tofu_apply(temp_path, "resilient")

    def test_get_public_domain_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            deployment_dir = root / "src" / "infra" / "terraform" / "deployments" / "local"
            deployment_dir.mkdir(parents=True)
            (deployment_dir / "deployment.yaml").write_text(
                "installation:\n  public_domain: mycorp.example.org\n", encoding="utf-8"
            )
            self.assertEqual(cluster_common.get_public_domain(root), "mycorp.example.org")
            self.assertEqual(cluster_common.get_intranet_domain(root), "corp.mycorp.example.org")
            self.assertEqual(cluster_common.get_cluster_domain(root), "c.corp.mycorp.example.org")

    def test_get_public_domain_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                cluster_common.get_public_domain(root)
            with self.assertRaises(ValueError):
                cluster_common.get_intranet_domain(root)
            with self.assertRaises(ValueError):
                cluster_common.get_cluster_domain(root)


class TestTofuDockerContext(BaseLifecycleTestCase):
    """Keep local OpenTofu providers on the Docker daemon selected by the CLI."""

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch.object(cluster_common.subprocess, "run")
    def test_init_apply_destroy_follow_current_context(self, mock_run: mock.MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="colima\n")
        deployment = Path("/repository/deployments/local")

        cluster_common.run_tofu_init(deployment)
        cluster_common.run_tofu_apply(deployment, "standalone")
        cluster_common.run_tofu_destroy(deployment)

        tofu_calls = [call for call in mock_run.call_args_list if call.args[0][0] == "tofu"]
        self.assertEqual(len(tofu_calls), 3)
        for call in tofu_calls:
            self.assertEqual(call.kwargs["env"]["DOCKER_CONTEXT"], "colima")
        self.assertNotIn("DOCKER_CONTEXT", os.environ)

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_explicit_daemon_and_production_do_not_probe_context(
        self, mock_run: mock.MagicMock
    ) -> None:
        cases = [
            # keep-sorted start
            ("local", {"DOCKER_CONTEXT": "remote"}),
            ("local", {"DOCKER_HOST": "unix:///explicit/docker.sock"}),
            ("production", {}),
            # keep-sorted end
        ]
        for target, environment in cases:
            with self.subTest(target=target, environment=environment):
                mock_run.reset_mock()
                with mock.patch.dict(os.environ, environment, clear=True):
                    cluster_common.run_tofu_apply(Path("/deployments") / target, "standalone")

                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.args[0][0], "tofu")
                self.assertEqual(
                    mock_run.call_args.kwargs["env"],
                    environment if target == "local" else None,
                )

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch.object(cluster_common.subprocess, "run")
    def test_context_lookup_failure_aborts_before_apply(self, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, ["docker", "context", "show"])

        with self.assertRaises(subprocess.CalledProcessError):
            cluster_common.run_tofu_apply(Path("/deployments/local"), "standalone")

        self.assertEqual(len(mock_run.call_args_list), 1)
        self.assertEqual(mock_run.call_args.args[0][0], "docker")


class TestBootstrapStatus(BaseLifecycleTestCase):
    """Defend complete bootstrap checks against healthy-parent false positives."""

    def setUp(self) -> None:
        super().setUp()
        self.root: dict[str, Any] = {
            "kind": "Application",
            "metadata": {"name": "fleet-root", "namespace": "argocd"},
            "status": {
                "sync": {"status": "Synced"},
                "health": {"status": "Healthy"},
                "resources": [
                    {"kind": "ApplicationSet", "name": "cell-apps", "namespace": "argocd"}
                ],
            },
        }
        self.dispatcher: dict[str, Any] = {
            "kind": "ApplicationSet",
            "metadata": {"name": "cell-apps", "namespace": "argocd"},
            "status": {
                "conditions": [
                    {"type": "ParametersGenerated", "status": "True"},
                    {"type": "ResourcesUpToDate", "status": "True"},
                ],
                "resources": [{"kind": "Application", "name": "platform", "namespace": "argocd"}],
            },
        }
        self.child: dict[str, Any] = {
            "kind": "Application",
            "metadata": {"name": "platform", "namespace": "argocd"},
            "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
        }

    def test_healthy_root_does_not_hide_failed_child(self) -> None:
        child: dict[str, Any] = {
            "kind": "Application",
            "metadata": {"name": "oidc", "namespace": "argocd"},
            "status": {"sync": {"status": "Synced"}, "health": {"status": "Degraded"}},
        }
        pending = cluster_common.bootstrap_pending([self.root, self.dispatcher, self.child, child])
        self.assertTrue(any("oidc" in item and "Degraded" in item for item in pending))
        child["status"]["health"]["status"] = "Healthy"
        self.assertEqual(
            cluster_common.bootstrap_pending([self.root, self.dispatcher, self.child, child]), []
        )

    def test_missing_root_or_dispatcher_cannot_report_success(self) -> None:
        for items in ([], [self.root], [self.dispatcher]):
            with self.subTest(items=items):
                self.assertTrue(cluster_common.bootstrap_pending(items))

    def test_healthy_root_without_dispatcher_inventory_cannot_report_success(self) -> None:
        self.root["status"]["resources"] = []
        self.assertTrue(cluster_common.bootstrap_pending([self.root, self.dispatcher, self.child]))

    def test_generation_error_and_missing_generated_child_block_success(self) -> None:
        self.dispatcher["status"]["conditions"].append({"type": "ErrorOccurred", "status": "True"})
        self.assertTrue(cluster_common.bootstrap_pending([self.root, self.dispatcher, self.child]))
        self.dispatcher["status"]["conditions"].pop()
        self.dispatcher["status"]["resources"] = [
            {"kind": "Application", "namespace": "argocd", "name": "missing"}
        ]
        self.assertTrue(cluster_common.bootstrap_pending([self.root, self.dispatcher, self.child]))

    def test_fleet_dispatcher_cannot_pass_without_observed_children(self) -> None:
        for inventory in (None, []):
            with self.subTest(inventory=inventory):
                if inventory is None:
                    self.dispatcher["status"].pop("resources", None)
                else:
                    self.dispatcher["status"]["resources"] = inventory
                pending = cluster_common.bootstrap_pending([self.root, self.dispatcher])
                self.assertTrue(any("generated application inventory" in item for item in pending))

    def test_unreferenced_empty_dispatcher_does_not_block_fleet(self) -> None:
        optional: dict[str, Any] = {
            "kind": "ApplicationSet",
            "metadata": {"name": "optional", "namespace": "argocd"},
            "status": {"conditions": self.dispatcher["status"]["conditions"]},
        }
        self.assertEqual(
            cluster_common.bootstrap_pending([self.root, self.dispatcher, self.child, optional]), []
        )

    def test_missing_image_discovery_and_stale_conditions_block_bootstrap(self) -> None:
        warehouse: dict[str, Any] = {
            "kind": "Warehouse",
            "metadata": {"name": "web", "namespace": "apps-example", "generation": 2},
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "MissingImageReferences",
                        "observedGeneration": 2,
                    }
                ]
            },
        }
        items = [self.root, self.dispatcher, self.child, warehouse]
        self.assertTrue(
            any(
                "MissingImageReferences" in item for item in cluster_common.bootstrap_pending(items)
            )
        )
        warehouse["status"]["conditions"] = [
            {"type": kind, "status": "True", "observedGeneration": 1}
            for kind in ("Ready", "Healthy")
        ]
        self.assertTrue(cluster_common.bootstrap_pending(items))
        for condition in warehouse["status"]["conditions"]:
            condition["observedGeneration"] = 2
        self.assertEqual(cluster_common.bootstrap_pending(items), [])

    def test_only_manual_initial_promotion_can_defer_workload_readiness(self) -> None:
        stage: dict[str, Any] = {
            "kind": "Stage",
            "metadata": {"name": "prod", "namespace": "apps-example", "generation": 1},
            "status": {
                "autoPromotionEnabled": False,
                "conditions": [
                    {
                        "type": "Ready",
                        "reason": "NoFreight",
                        "status": "False",
                        "observedGeneration": 1,
                    }
                ],
            },
        }
        app: dict[str, Any] = {
            "kind": "Application",
            "metadata": {
                "name": "prod",
                "namespace": "argocd",
                "annotations": {"kargo.akuity.io/authorized-stage": "apps-example:prod"},
            },
            "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Missing"}},
        }
        items = [self.root, self.dispatcher, self.child, stage, app]
        self.assertEqual(cluster_common.bootstrap_pending(items), [])
        condition_updates: tuple[dict[str, Any], ...] = (
            {"observedGeneration": 0},
            {"observedGeneration": None},
            {"status": "True"},
        )
        for condition_update in condition_updates:
            with self.subTest(condition_update=condition_update):
                condition = stage["status"]["conditions"][0]
                original = condition.copy()
                condition.update(condition_update)
                self.assertTrue(cluster_common.bootstrap_pending(items))
                condition.clear()
                condition.update(original)
        application_updates: tuple[dict[str, Any], ...] = (
            {"sync": {"status": "Unknown"}},
            {"conditions": [{"type": "ComparisonError"}]},
            {"conditions": [{"type": "InvalidSpecError"}]},
            {"operationState": {"phase": "Running"}},
            {"operationState": {"phase": "Terminating"}},
            {"operationState": {"phase": "Failed"}},
            {"operationState": {"phase": "Error"}},
        )
        for application_update in application_updates:
            with self.subTest(application_update=application_update):
                original_status = app["status"].copy()
                app["status"].update(application_update)
                self.assertTrue(cluster_common.bootstrap_pending(items))
                app["status"] = original_status
        stage["status"]["autoPromotionEnabled"] = True
        self.assertTrue(cluster_common.bootstrap_pending(items))
        stage["status"]["autoPromotionEnabled"] = False
        stage["status"]["freightHistory"] = [{"id": "already-promoted"}]
        self.assertTrue(cluster_common.bootstrap_pending(items))

    def test_running_sync_hook_cannot_hide_behind_healthy_application(self) -> None:
        self.root["status"]["operationState"] = {"phase": "Running"}
        self.assertTrue(cluster_common.bootstrap_pending([self.root, self.dispatcher, self.child]))

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_comparison_refresh_is_batched_and_requires_a_fresh_status_read(
        self, command: mock.MagicMock
    ) -> None:
        self.child["status"] = {
            "sync": {"status": "Unknown"},
            "health": {"status": "Healthy"},
            "conditions": [{"type": "ComparisonError"}],
        }
        second: dict[str, Any] = {
            "kind": "Application",
            "metadata": {"name": "second", "namespace": "argocd"},
            "status": self.child["status"],
        }
        items = [self.root, self.dispatcher, self.child, second]
        command.side_effect = [
            subprocess.CompletedProcess([], 0, json.dumps({"items": items}), ""),
            subprocess.CompletedProcess([], 0, "annotated", ""),
        ]
        code, status, _ = cluster_common.query_argocd_status("fixture", refresh_pending=True)
        self.assertEqual(code, 0)
        self.assertIn("Unknown:Healthy", status)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(
            command.call_args.args[0],
            [
                "kubectl",
                "--context",
                "fixture",
                "--request-timeout=15s",
                "-n",
                "argocd",
                "annotate",
                "applications.argoproj.io",
                "platform",
                "second",
                "argocd.argoproj.io/refresh=normal",
                "--overwrite",
            ],
        )
        self.assertEqual(command.call_args.kwargs["timeout"], 20)
        self.child["status"]["sync"]["status"] = "Synced"
        self.child["status"]["conditions"] = []
        command.side_effect = [subprocess.CompletedProcess([], 0, json.dumps({"items": items}), "")]
        self.assertEqual(
            cluster_common.query_argocd_status("fixture", refresh_pending=True),
            (0, "Synced:Healthy", ""),
        )
        self.assertEqual(command.call_count, 3)

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_refresh_checks_stale_health_without_refreshing_manual_promotion(
        self, command: mock.MagicMock
    ) -> None:
        self.child["status"]["health"]["status"] = "Progressing"
        stage: dict[str, Any] = {
            "kind": "Stage",
            "metadata": {"name": "prod", "namespace": "apps-example", "generation": 1},
            "status": {
                "autoPromotionEnabled": False,
                "conditions": [
                    {
                        "type": "Ready",
                        "reason": "NoFreight",
                        "status": "False",
                        "observedGeneration": 1,
                    }
                ],
            },
        }
        manual: dict[str, Any] = {
            "kind": "Application",
            "metadata": {
                "name": "prod",
                "namespace": "argocd",
                "annotations": {"kargo.akuity.io/authorized-stage": "apps-example:prod"},
            },
            "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Missing"}},
        }
        items = [self.root, self.dispatcher, self.child, stage, manual]
        command.side_effect = [
            subprocess.CompletedProcess([], 0, json.dumps({"items": items}), ""),
            subprocess.CompletedProcess([], 0, "annotated", ""),
        ]
        code, status, _ = cluster_common.query_argocd_status("fixture", refresh_pending=True)
        self.assertEqual(code, 0)
        self.assertEqual(status, "Pending: Application/argocd/platform: Synced:Progressing")
        refresh_command = command.call_args.args[0]
        self.assertIn("platform", refresh_command)
        self.assertNotIn("prod", refresh_command)
        self.assertEqual(command.call_count, 2)

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_refreshes_stale_out_of_sync_comparison_when_workload_is_healthy(
        self, command: mock.MagicMock
    ) -> None:
        self.child["status"]["sync"]["status"] = "OutOfSync"
        command.side_effect = [
            subprocess.CompletedProcess(
                [], 0, json.dumps({"items": [self.root, self.dispatcher, self.child]}), ""
            ),
            subprocess.CompletedProcess([], 0, "annotated", ""),
        ]
        code, status, _ = cluster_common.query_argocd_status("fixture", refresh_pending=True)
        self.assertEqual(code, 0)
        self.assertEqual(status, "Pending: Application/argocd/platform: OutOfSync:Healthy")
        self.assertEqual(command.call_count, 2)
        self.assertIn("platform", command.call_args.args[0])
        self.assertIn("argocd.argoproj.io/refresh=normal", command.call_args.args[0])

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_comparison_refresh_failure_cannot_report_success(
        self, command: mock.MagicMock
    ) -> None:
        self.child["status"] = {
            "sync": {"status": "Unknown"},
            "health": {"status": "Healthy"},
            "conditions": [{"type": "ComparisonError"}],
        }
        snapshot = subprocess.CompletedProcess(
            [], 0, json.dumps({"items": [self.root, self.dispatcher, self.child]}), ""
        )
        for failure in (
            subprocess.CompletedProcess([], 1, "", "annotation denied"),
            subprocess.TimeoutExpired(["kubectl"], 20),
        ):
            with self.subTest(failure=type(failure).__name__):
                command.side_effect = [snapshot, failure]
                code, status, error = cluster_common.query_argocd_status(
                    "fixture", refresh_pending=True
                )
                self.assertNotEqual(code, 0)
                self.assertEqual(status, "")
                self.assertIn("refresh", error)
        command.reset_mock()
        command.side_effect = [snapshot]
        self.assertIn("Unknown:Healthy", cluster_common.query_argocd_status("fixture")[1])
        command.assert_called_once()

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_kubectl_status_uses_full_resource_inventory(self, command: mock.MagicMock) -> None:
        command.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"items": [self.root, self.dispatcher, self.child]}), ""
        )
        self.assertEqual(cluster_common.query_argocd_status("fixture"), (0, "Synced:Healthy", ""))
        command.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"items": [self.root]}), ""
        )
        self.assertIn("missing", cluster_common.query_argocd_status("fixture")[1])


class TestArgoCDWaitLoop(BaseLifecycleTestCase):
    """Test Argo CD synchronization polling loop and backoff."""

    @mock.patch.object(cluster_common, "query_argocd_status")
    def test_argocd_wait_immediate_success(self, mock_query: mock.MagicMock) -> None:
        mock_query.return_value = (0, "Synced:Healthy", "")
        mock_sleep = mock.MagicMock()

        result = cluster_common.wait_for_argocd_sync(
            context="ctrl-eaws-lh1",
            timeout=60.0,
            options=cluster_common.PollOptions(sleep_fn=mock_sleep),
        )
        self.assertTrue(result)
        mock_query.assert_called_once_with("ctrl-eaws-lh1", refresh_pending=True)
        mock_sleep.assert_not_called()

    @mock.patch.object(cluster_common, "query_argocd_status")
    def test_comparison_refresh_is_throttled_and_failed_refresh_keeps_waiting(
        self, query: mock.MagicMock
    ) -> None:
        now = 0.0

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        query.side_effect = [
            (1, "", "Application status refresh failed"),
            (0, "Pending: Unknown:Healthy", ""),
            (0, "Pending: Unknown:Healthy", ""),
            (0, "Pending: Unknown:Healthy", ""),
            (0, "Synced:Healthy", ""),
        ]
        self.assertTrue(
            cluster_common.wait_for_argocd_sync(
                "fixture",
                timeout=60,
                options=cluster_common.PollOptions(
                    initial_interval=10,
                    backoff_factor=1,
                    max_interval=10,
                    sleep_fn=advance,
                    time_fn=lambda: now,
                ),
            )
        )
        self.assertEqual(now, 40)
        self.assertEqual(
            query.call_args_list,
            [
                mock.call("fixture", refresh_pending=refresh)
                for refresh in (True, False, False, True, False)
            ],
        )

    @mock.patch.object(cluster_common, "query_argocd_status")
    def test_argocd_wait_retry_then_success(self, mock_query: mock.MagicMock) -> None:
        mock_query.side_effect = [
            (1, "", "connection refused"),
            (0, "OutOfSync:Progressing", ""),
            (0, "Synced:Healthy", ""),
        ]
        mock_sleep = mock.MagicMock()

        result = cluster_common.wait_for_argocd_sync(
            context="ctrl-eaws-lh1",
            timeout=60.0,
            options=cluster_common.PollOptions(
                initial_interval=5.0,
                backoff_factor=1.5,
                sleep_fn=mock_sleep,
            ),
        )
        self.assertTrue(result)
        self.assertEqual(mock_query.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        # Check initial interval 5.0 and backed off 5.0 * 1.5 = 7.5
        mock_sleep.assert_has_calls([mock.call(5.0), mock.call(7.5)])

    @mock.patch.object(cluster_common, "query_argocd_status")
    def test_argocd_wait_timeout_failure(self, mock_query: mock.MagicMock) -> None:
        mock_query.return_value = (0, "OutOfSync:Progressing", "")

        simulated_times = [0.0, 5.0, 11.0]
        time_index = 0

        def fake_time() -> float:
            nonlocal time_index
            t = simulated_times[min(time_index, len(simulated_times) - 1)]
            time_index += 1
            return t

        with self.assertRaises(TimeoutError) as ctx:
            cluster_common.wait_for_argocd_sync(
                context="ctrl-eaws-lh1",
                timeout=10.0,
                options=cluster_common.PollOptions(
                    initial_interval=5.0,
                    sleep_fn=lambda _: None,
                    time_fn=fake_time,
                ),
            )
        self.assertIn("Timed out after 10s", str(ctx.exception))
        self.assertIn("OutOfSync:Progressing", str(ctx.exception))

    def test_context_derivation(self) -> None:
        self.assertEqual(cluster_common.get_context_for_target("local"), "ctrl-eaws-lh1")
        self.assertEqual(cluster_common.get_context_for_target("production"), "ctrl-aws-usw2")
        self.assertEqual(cluster_common.get_context_for_target("custom_cell"), "ctrl-custom_cell")

    def test_get_public_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dep_path = root / "src" / "infra" / "terraform" / "deployments" / "local"
            dep_path.mkdir(parents=True)
            (dep_path / "deployment.yaml").write_text(
                "installation:\n  public_domain: test.example.com\n"
            )
            self.assertEqual(cluster_common.get_public_domain(root), "test.example.com")


class TestTofuDestroy(BaseLifecycleTestCase):
    """Test OpenTofu destroy invocation and cluster_down workflow."""

    @mock.patch.object(cluster_common.subprocess, "run")
    def test_tofu_destroy_command(self, mock_run: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            cluster_common.run_tofu_destroy(temp_path, timeout=180.0)

            expected_cmd = ["tofu", f"-chdir={temp_path}", "destroy", "-auto-approve"]
            mock_run.assert_called_once_with(expected_cmd, check=True, timeout=180.0, env=None)

    def test_forgets_only_historical_runtime_before_destroy(self) -> None:
        cases = [
            ("no state", "", False),
            ("empty state", json.dumps({"resources": []}), False),
            (
                "cloud resources",
                json.dumps({"resources": [{"module": "module.control_plane"}]}),
                False,
            ),
            (
                "historical runtime",
                json.dumps({"resources": [{"module": "module.floci_runtime"}]}),
                True,
            ),
        ]
        deployment = Path("/deployments/local")
        for name, state, should_forget in cases:
            with self.subTest(name=name):
                with (
                    mock.patch.object(cluster_down, "tofu_environment", return_value={}),
                    mock.patch.object(cluster_down.subprocess, "run") as run,
                ):
                    run.return_value = subprocess.CompletedProcess([], 0, stdout=state)
                    cluster_down.forget_floci_runtime(deployment, 100)

                commands = [call.args[0] for call in run.call_args_list]
                expected = [["tofu", f"-chdir={deployment}", "state", "pull"]]
                if should_forget:
                    expected.append([
                        "tofu",
                        f"-chdir={deployment}",
                        "state",
                        "rm",
                        "module.floci_runtime",
                    ])
                self.assertEqual(commands, expected)

    @mock.patch.object(cluster_down, "run_tofu_init")
    @mock.patch.object(cluster_down, "ensure_deployment_dir")
    @mock.patch.object(cluster_down, "run_tofu_destroy")
    def test_cloud_down_destroys_without_starting_local_runtime(
        self,
        mock_destroy: mock.MagicMock,
        mock_ensure_dir: mock.MagicMock,
        mock_init: mock.MagicMock,
    ) -> None:
        fake_path = Path("/tmp/fake/deployment")
        mock_ensure_dir.return_value = fake_path
        with (
            mock.patch.object(cluster_down.runtime, "start") as mock_start,
            mock.patch.object(cluster_down.runtime, "stop") as mock_stop,
        ):
            cluster_down.cluster_down(target="production", timeout=200)

        mock_ensure_dir.assert_called_once_with("production", None)
        mock_init.assert_called_once_with(fake_path)
        mock_destroy.assert_called_once_with(fake_path, timeout=200.0)
        mock_start.assert_not_called()
        mock_stop.assert_not_called()


class TestLocalImageSeed(BaseLifecycleTestCase):
    """Keep local image publication ordered, hermetic, and confined to the local registry."""

    def test_seed_wrapper_clears_inherited_developer_tag_before_publication(self) -> None:
        files = cluster_up.runfiles.Create()
        assert files is not None
        executable = files.Rlocation(os.environ[cluster_up.SEED_RLOCATIONPATH_VARIABLE])
        library = files.Rlocation("bazel_tools/tools/bash/runfiles/runfiles.bash")
        assert executable is not None
        assert library is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runfiles_library = root / "bazel_tools/tools/bash/runfiles/runfiles.bash"
            runfiles_library.parent.mkdir(parents=True)
            runfiles_library.symlink_to(library)
            publisher = root / "_main/src/infra/images/seed_images.bash"
            publisher.parent.mkdir(parents=True)
            publisher.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$WORKLOAD_TAG_PREFIX" "$WORKLOAD_STREAM_TAG"\n',
                encoding="utf-8",
            )
            publisher.chmod(0o700)
            environment = dict(os.environ)
            environment.update({
                "RUNFILES_DIR": str(root),
                "RUNFILES_MANIFEST_FILE": "",
                "WORKLOAD_STREAM_TAG": "dev-20260901T120000Z_0123456789ab",
                "WORKLOAD_TAG_PREFIX": "dev",
            })
            result = subprocess.run(
                [executable], env=environment, check=True, capture_output=True, text=True
            )
            self.assertEqual(result.stdout.splitlines(), ["ci", ""])

    def test_seed_executes_declared_runfile_and_overrides_remote_registry_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "_main/src/infra/images/seed.bash"
            executable.parent.mkdir(parents=True)
            executable.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$WORKLOAD_REGISTRY" '
                '"$WORKLOAD_REGISTRY_INSECURE" "$RUNFILES_DIR" > "$SEED_RESULT"\n'
            )
            executable.chmod(0o700)
            result = root / "seed-result"
            with mock.patch.dict(
                os.environ,
                {
                    "RUNFILES_DIR": str(root),
                    "RUNFILES_MANIFEST_FILE": "",
                    cluster_up.SEED_RLOCATIONPATH_VARIABLE: "_main/src/infra/images/seed.bash",
                    "WORKLOAD_REGISTRY": "remote.invalid/production",
                    "WORKLOAD_REGISTRY_INSECURE": "false",
                    "SEED_RESULT": str(result),
                },
            ):
                cluster_up.seed_local_images()

            self.assertEqual(
                result.read_text().splitlines(),
                ["127.0.0.1:15100/000000000000/us-east-1", "true", str(root)],
            )

    def test_missing_seed_runfiles_fail_before_publishing(self) -> None:
        for files in (
            None,
            mock.Mock(Rlocation=mock.Mock(return_value=None)),
            mock.Mock(Rlocation=mock.Mock(return_value="/missing/local/seed.bash")),
        ):
            with (
                self.subTest(files=files),
                mock.patch.object(cluster_up.runfiles, "Create", return_value=files),
                mock.patch.object(cluster_up.subprocess, "run") as launch,
                self.assertRaises(RuntimeError),
            ):
                cluster_up.seed_local_images()
            launch.assert_not_called()

    def test_undeclared_seed_location_fails_before_publishing(self) -> None:
        with (
            mock.patch.dict(os.environ),
            mock.patch.object(cluster_up.subprocess, "run") as launch,
            self.assertRaises(RuntimeError),
        ):
            del os.environ[cluster_up.SEED_RLOCATIONPATH_VARIABLE]
            cluster_up.seed_local_images()
        launch.assert_not_called()

    def test_seed_failure_is_reported_by_cli(self) -> None:
        with (
            mock.patch.object(
                cluster_up,
                "cluster_up",
                side_effect=subprocess.CalledProcessError(1, ["seed.bash"]),
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            status = cluster_up.main([])
        self.assertEqual(status, 1)
        self.assertIn("Cluster bring-up failed:", stderr.getvalue())
        self.assertIn("seed.bash", stderr.getvalue())

    def test_seed_failure_prevents_health_success_and_tailnet_enrollment(self) -> None:
        with (
            mock.patch.object(cluster_up, "find_repo_root", return_value=Path("/repository")),
            mock.patch.object(cluster_up, "ensure_deployment_dir"),
            mock.patch.object(cluster_up.runtime, "ensure_docker_local"),
            mock.patch.object(cluster_up.runtime, "start"),
            mock.patch.object(cluster_up.runtime, "reconcile_registry"),
            mock.patch.object(cluster_up.kubeconfig, "project"),
            mock.patch.object(cluster_up, "run_tofu_init"),
            mock.patch.object(cluster_up, "run_tofu_apply"),
            mock.patch.object(
                cluster_up,
                "seed_local_images",
                side_effect=subprocess.CalledProcessError(1, ["seed"]),
            ),
            mock.patch.object(cluster_up, "wait_for_argocd_sync") as wait,
            mock.patch.object(cluster_tailnet, "TailnetManager") as tailnet,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            cluster_up.cluster_up(cluster_up.ClusterUpConfig())
        wait.assert_not_called()
        tailnet.assert_not_called()


class TestLocalLifecycle(BaseLifecycleTestCase):
    """Protect retained local state and keep the emulator available during reconciliation."""

    def test_start_prepares_runtime_before_tofu_and_tailnet_after_sync(self) -> None:
        for skip_wait in (False, True):
            with self.subTest(skip_wait=skip_wait):
                self._assert_start_order(skip_wait=skip_wait)

    def test_tailnet_failure_fails_cli_unless_explicitly_disabled(self) -> None:
        failures = [
            (RuntimeError("bridge unavailable"), "bridge unavailable"),
            (OSError("tailscale executable unavailable"), "tailscale executable unavailable"),
            (
                subprocess.CalledProcessError(23, ["tailscale", "--authkey=test-secret"]),
                "Tailnet enrollment failed with exit status 23",
            ),
        ]
        for failure, message in failures:
            for disabled in (False, True):
                with (
                    self.subTest(failure=type(failure).__name__, disabled=disabled),
                    mock.patch.object(
                        cluster_up, "find_repo_root", return_value=Path("/repository")
                    ),
                    mock.patch.object(cluster_up, "ensure_deployment_dir"),
                    mock.patch.object(cluster_up.runtime, "ensure_docker_local"),
                    mock.patch.object(cluster_up.runtime, "start"),
                    mock.patch.object(cluster_up.runtime, "reconcile_registry"),
                    mock.patch.object(cluster_up.kubeconfig, "project"),
                    mock.patch.object(cluster_up, "run_tofu_init"),
                    mock.patch.object(cluster_up, "run_tofu_apply"),
                    mock.patch.object(cluster_up, "seed_local_images"),
                    mock.patch.object(cluster_up.runtime, "reconcile_tls"),
                    mock.patch.object(cluster_up.headscale_keys, "reconcile"),
                    mock.patch.object(cluster_up, "wait_for_argocd_sync") as health,
                    mock.patch.object(cluster_tailnet, "TailnetManager") as tailnet,
                    mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
                ):
                    tailnet.return_value.up.side_effect = failure
                    status = cluster_up.main(["--no-tailnet"] if disabled else [])

                health.assert_called_once()
                self.assertEqual(status, 0 if disabled else 1)
                self.assertNotIn("test-secret", stderr.getvalue())
                if disabled:
                    tailnet.assert_not_called()
                    self.assertEqual(stderr.getvalue(), "")
                else:
                    tailnet.return_value.up.assert_called_once()
                    self.assertIn(message, stderr.getvalue())

    def _assert_start_order(self, *, skip_wait: bool) -> None:
        calls = mock.Mock()
        root = Path("/repository")
        with tempfile.TemporaryDirectory() as directory:
            deployment = Path(directory)
            with (
                mock.patch.object(cluster_up, "find_repo_root", return_value=root),
                mock.patch.object(cluster_up, "ensure_deployment_dir", return_value=deployment),
                mock.patch.object(cluster_up.runtime, "ensure_docker_local", calls.docker),
                mock.patch.object(cluster_up.runtime, "start", calls.runtime),
                mock.patch.object(cluster_up.runtime, "reconcile_registry", calls.registry),
                mock.patch.object(cluster_up.kubeconfig, "project", calls.kubeconfig),
                mock.patch.object(cluster_up, "seed_local_images", calls.seed),
                mock.patch.object(cluster_up.runtime, "reconcile_tls", calls.tls),
                mock.patch.object(cluster_up.headscale_keys, "reconcile", calls.keys),
                mock.patch.object(cluster_up, "run_tofu_init", calls.init),
                mock.patch.object(cluster_up, "run_tofu_apply", calls.apply),
                mock.patch.object(cluster_up, "wait_for_argocd_sync", calls.sync),
                mock.patch.object(cluster_tailnet, "TailnetManager", calls.tailnet),
            ):
                cluster_up.cluster_up(cluster_up.ClusterUpConfig(timeout=123, skip_wait=skip_wait))

            self.assertEqual(
                calls.mock_calls,
                [
                    mock.call.docker(root, require_capacity=True),
                    mock.call.runtime(root, timeout=123),
                    mock.call.init(deployment),
                    mock.call.apply(deployment, "standalone"),
                    mock.call.registry(root),
                    mock.call.kubeconfig(root),
                    mock.call.seed(),
                    mock.call.tls(root, timeout=123),
                    mock.call.keys(root, timeout=123),
                    *([] if skip_wait else [mock.call.sync("ctrl-eaws-lh1", timeout=123.0)]),
                    mock.call.tailnet(),
                    mock.call.tailnet().up(),
                ],
            )

    @mock.patch.object(cluster_up.runtime, "ensure_docker_local")
    @mock.patch.object(cluster_up.runtime, "start", side_effect=RuntimeError("runtime failed"))
    @mock.patch.object(cluster_up, "run_tofu_init")
    @mock.patch.object(cluster_up, "run_tofu_apply")
    def test_start_failure_prevents_tofu_reconciliation(
        self,
        mock_apply: mock.MagicMock,
        mock_init: mock.MagicMock,
        _mock_start: mock.MagicMock,
        _mock_docker: mock.MagicMock,
    ) -> None:
        with self.assertRaises(RuntimeError):
            cluster_up.cluster_up(cluster_up.ClusterUpConfig(skip_wait=True))

        mock_init.assert_not_called()
        mock_apply.assert_not_called()

    def test_stop_preserves_state_without_running_tofu(self) -> None:
        calls = mock.Mock()
        root = Path("/repository")
        with (
            mock.patch.object(cluster_tailnet, "TailnetManager", calls.tailnet),
            mock.patch.object(cluster_down.runtime, "stop", calls.stop),
            mock.patch.object(cluster_down.runtime, "start", calls.start),
            mock.patch.object(cluster_down, "run_tofu_init", calls.init),
            mock.patch.object(cluster_down, "forget_floci_runtime", calls.forget),
            mock.patch.object(cluster_down, "run_tofu_destroy", calls.destroy),
        ):
            cluster_down.cluster_down(repo_root=root, timeout=100)

        self.assertEqual(
            calls.mock_calls,
            [
                mock.call.tailnet(repo_root=root),
                mock.call.tailnet().down(),
                mock.call.stop(root, timeout=100),
            ],
        )

    def test_destroy_keeps_runtime_running_until_tofu_finishes(self) -> None:
        calls = mock.Mock()
        root = Path("/repository")
        with tempfile.TemporaryDirectory() as directory:
            deployment = Path(directory)
            with (
                mock.patch.object(cluster_tailnet, "TailnetManager", calls.tailnet),
                mock.patch.object(cluster_down, "ensure_deployment_dir", return_value=deployment),
                mock.patch.object(cluster_down.runtime, "ensure_docker_local", calls.docker),
                mock.patch.object(cluster_down.runtime, "start", calls.start),
                mock.patch.object(cluster_down.runtime, "stop", calls.stop),
                mock.patch.object(
                    cluster_down.runtime, "cluster_volumes", calls.volumes
                ) as volumes,
                mock.patch.object(cluster_down.runtime, "remove_volumes", calls.remove),
                mock.patch.object(cluster_down, "run_tofu_init", calls.init),
                mock.patch.object(cluster_down, "forget_floci_runtime", calls.forget),
                mock.patch.object(cluster_down, "run_tofu_destroy", calls.destroy),
            ):
                volumes.return_value = ["floci-fleet-eks-cell"]
                cluster_down.cluster_down(repo_root=root, timeout=100, destroy=True)

            self.assertEqual(
                calls.mock_calls,
                [
                    mock.call.tailnet(repo_root=root),
                    mock.call.tailnet().down(),
                    mock.call.docker(root),
                    mock.call.start(root, timeout=100),
                    mock.call.init(deployment),
                    mock.call.forget(deployment, 100),
                    mock.call.volumes(root),
                    mock.call.destroy(deployment, timeout=100.0),
                    mock.call.stop(root, timeout=100),
                    mock.call.remove(["floci-fleet-eks-cell"]),
                ],
            )

    def test_failed_destroy_does_not_stop_runtime(self) -> None:
        cases = [
            # keep-sorted start
            "destroy",
            "forget",
            "start",
            # keep-sorted end
        ]
        for failing_step in cases:
            with self.subTest(failing_step=failing_step):
                with (
                    mock.patch.object(cluster_tailnet, "TailnetManager"),
                    mock.patch.object(cluster_down.runtime, "ensure_docker_local"),
                    mock.patch.object(cluster_down, "run_tofu_init"),
                    mock.patch.object(cluster_down, "forget_floci_runtime") as mock_forget,
                    mock.patch.object(cluster_down.runtime, "start") as mock_start,
                    mock.patch.object(cluster_down.runtime, "stop") as mock_stop,
                    mock.patch.object(cluster_down.runtime, "cluster_volumes"),
                    mock.patch.object(cluster_down.runtime, "remove_volumes") as mock_remove,
                    mock.patch.object(cluster_down, "ensure_deployment_dir"),
                    mock.patch.object(cluster_down, "run_tofu_destroy") as mock_destroy,
                ):
                    failing_call = {
                        "destroy": mock_destroy,
                        "forget": mock_forget,
                        "start": mock_start,
                    }[failing_step]
                    failing_call.side_effect = RuntimeError("lifecycle failed")

                    with self.assertRaises(RuntimeError):
                        cluster_down.cluster_down(destroy=True)

                    mock_stop.assert_not_called()
                    mock_remove.assert_not_called()
                    if failing_step != "destroy":
                        mock_destroy.assert_not_called()


class TestMainEntryPoints(BaseLifecycleTestCase):
    """Test CLI main() entry points and exit status."""

    @mock.patch.object(cluster_up, "cluster_up")
    def test_cluster_up_main_success(self, mock_cluster_up: mock.MagicMock) -> None:
        exit_code = cluster_up.main(["--target", "local", "--skip-wait"])
        self.assertEqual(exit_code, 0)
        mock_cluster_up.assert_called_once_with(
            cluster_up.ClusterUpConfig(
                target="local",
                availability="standalone",
                timeout=600,
                skip_wait=True,
                context=None,
                with_tailnet=True,
            )
        )

    @mock.patch.object(cluster_up, "cluster_up", side_effect=RuntimeError("something went wrong"))
    def test_cluster_up_main_failure(self, mock_cluster_up: mock.MagicMock) -> None:
        with mock.patch("sys.stderr"):
            exit_code = cluster_up.main(["--target", "local"])
        self.assertEqual(exit_code, 1)

    @mock.patch.object(cluster_down, "cluster_down")
    def test_cluster_down_main_success(self, mock_cluster_down: mock.MagicMock) -> None:
        exit_code = cluster_down.main(["--target", "production", "--timeout", "100"])
        self.assertEqual(exit_code, 0)
        mock_cluster_down.assert_called_once_with(
            target="production",
            timeout=100,
            destroy=False,
        )

    @mock.patch.object(cluster_down, "cluster_down")
    def test_cluster_destroy_main_success(self, mock_cluster_down: mock.MagicMock) -> None:
        exit_code = cluster_down.main(["--destroy"])
        self.assertEqual(exit_code, 0)
        mock_cluster_down.assert_called_once_with(target="local", timeout=300, destroy=True)

    @mock.patch.object(cluster_down, "cluster_down", side_effect=RuntimeError("destroy failed"))
    def test_cluster_down_main_failure(self, mock_cluster_down: mock.MagicMock) -> None:
        with mock.patch("sys.stderr"):
            exit_code = cluster_down.main(["--target", "local"])
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
