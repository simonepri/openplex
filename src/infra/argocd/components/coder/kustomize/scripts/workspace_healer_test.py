#!/usr/bin/env python3
"""Tests for Coder workspace healer core engine, CLI, and state tracker."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from coder_api import BootstrapError
from workspace_healer import (
    OPT_OUT_ANNOTATION,
    RateLimiter,
    StateTracker,
    WorkspaceHealer,
    is_opted_out,
    main,
    parse_args,
    read_token_safely,
    run_continuous_loop,
    touch_heartbeat,
)


def make_workspace(
    workspace_id: str = "ws-1",
    owner_name: str = "alice",
    status: str = "running",
    transition: str = "start",
    agent_statuses: list[str] | None = None,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    job_error: str = "",
) -> dict[str, Any]:
    agents = []
    if agent_statuses is not None:
        agents = [{"id": f"agent-{i}", "status": st} for i, st in enumerate(agent_statuses)]

    build: dict[str, Any] = {
        "id": "build-1",
        "transition": transition,
        "status": status,
        "job": {"error": job_error, "status": "failed" if status == "failed" else "succeeded"},
        "resources": [{"name": "compute", "agents": agents}],
    }

    workspace: dict[str, Any] = {
        "id": workspace_id,
        "name": f"name-{workspace_id}",
        "owner_name": owner_name,
        "latest_build": build,
    }
    if annotations is not None:
        workspace["annotations"] = annotations
    if labels is not None:
        workspace["labels"] = labels
    return workspace


class TestSafeTokenReading(unittest.TestCase):
    def test_token_file_read_resolves_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            real_file = Path(tmp_dir) / "real_token"
            real_file.write_text("secret-session-token\n")
            link_file = Path(tmp_dir) / "token_symlink"
            Path(link_file).symlink_to(real_file)

            token = read_token_safely(link_file)
            self.assertEqual(token, "secret-session-token")

    def test_token_file_read_fails_when_symlink_is_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_target = Path(tmp_dir) / "nonexistent"
            link_file = Path(tmp_dir) / "broken_symlink"
            Path(link_file).symlink_to(missing_target)

            with self.assertRaises(ValueError):
                read_token_safely(link_file)

    def test_token_file_read_fails_when_path_is_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(ValueError):
                read_token_safely(tmp_dir)

    def test_token_file_read_fails_when_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            empty_file = Path(tmp_dir) / "empty_token"
            empty_file.write_text("   \n")

            with self.assertRaises(ValueError):
                read_token_safely(empty_file)

    def test_token_file_read_succeeds_and_unlinks_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            token_file = Path(tmp_dir) / "session_token"
            token_file.write_text("valid-coder-token\n")

            token = read_token_safely(token_file, unlink=True)

            self.assertEqual(token, "valid-coder-token")
            self.assertFalse(token_file.exists())


class TestOptOutAnnotation(unittest.TestCase):
    def test_is_opted_out_returns_true_for_workspace_annotation(self) -> None:
        ws = make_workspace(annotations={OPT_OUT_ANNOTATION: "true"})
        self.assertTrue(is_opted_out(ws))

    def test_is_opted_out_returns_true_for_workspace_label(self) -> None:
        ws = make_workspace(labels={"healer.coder.openplex.dev/opt-out": "yes"})
        self.assertTrue(is_opted_out(ws))

    def test_is_opted_out_returns_true_for_resource_metadata(self) -> None:
        ws = make_workspace()
        ws["latest_build"]["resources"] = [
            {"metadata": [{"key": OPT_OUT_ANNOTATION, "value": "1"}]}
        ]
        self.assertTrue(is_opted_out(ws))

    def test_is_opted_out_returns_false_when_unannotated(self) -> None:
        ws = make_workspace()
        self.assertFalse(is_opted_out(ws))


class TestStateTracker(unittest.TestCase):
    def test_state_tracker_increments_misses_and_resets_healthy(self) -> None:
        tracker = StateTracker()
        self.assertEqual(tracker.get_misses("ws-1"), 0)

        miss1 = tracker.record_miss("ws-1")
        miss2 = tracker.record_miss("ws-1")
        self.assertEqual(miss1, 1)
        self.assertEqual(miss2, 2)
        self.assertEqual(tracker.get_misses("ws-1"), 2)

        tracker.record_healthy("ws-1")
        self.assertEqual(tracker.get_misses("ws-1"), 0)

    def test_state_tracker_prunes_stale_workspaces(self) -> None:
        tracker = StateTracker()
        tracker.record_miss("ws-1")
        tracker.record_miss("ws-2")

        tracker.prune({"ws-1"})
        self.assertEqual(tracker.get_misses("ws-1"), 1)
        self.assertEqual(tracker.get_misses("ws-2"), 0)

    def test_state_tracker_persists_and_restores_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            tracker = StateTracker(state_file)
            tracker.record_miss("ws-1")
            tracker.record_miss("ws-1")
            tracker.save()

            restored = StateTracker(state_file)
            self.assertEqual(restored.get_misses("ws-1"), 2)


class TestWorkspaceHealerDecisions(unittest.TestCase):
    def setUp(self) -> None:
        self.healer = WorkspaceHealer(
            coder_url="https://coder.example.com",
            session_token="test-token",
            rate_limiter=RateLimiter(0.0),
            consecutive_miss_threshold=3,
            failed_miss_threshold=1,
            max_heals_per_run=3,
        )

    def test_healer_skips_when_opt_out_present(self) -> None:
        ws = make_workspace(
            workspace_id="ws-optout",
            status="running",
            agent_statuses=["disconnected"],
            annotations={OPT_OUT_ANNOTATION: "true"},
        )
        self.healer.coder_api.request = MagicMock(return_value=(200, [ws]))  # type: ignore[method-assign]

        decisions = self.healer.heal_workspaces()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action"], "skip")
        self.assertEqual(decisions[0]["workspace_id"], "ws-optout")

    def test_healer_skips_when_transition_is_not_start(self) -> None:
        ws = make_workspace(
            workspace_id="ws-stop",
            status="running",
            transition="stop",
            agent_statuses=["disconnected"],
        )
        self.healer.coder_api.request = MagicMock(return_value=(200, [ws]))  # type: ignore[method-assign]

        decisions = self.healer.heal_workspaces()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action"], "skip")

    def test_healer_skips_when_status_in_progress(self) -> None:
        ws = make_workspace(
            workspace_id="ws-starting",
            status="starting",
            transition="start",
        )
        self.healer.coder_api.request = MagicMock(return_value=(200, [ws]))  # type: ignore[method-assign]

        decisions = self.healer.heal_workspaces()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action"], "skip")

    def test_healer_skips_healthy_running_workspace_with_connected_agents(self) -> None:
        ws = make_workspace(
            workspace_id="ws-healthy",
            status="running",
            transition="start",
            agent_statuses=["connected"],
        )
        self.healer.coder_api.request = MagicMock(return_value=(200, [ws]))  # type: ignore[method-assign]

        decisions = self.healer.heal_workspaces()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action"], "skip")
        self.assertEqual(decisions[0]["consecutive_misses"], 0)

    def test_healer_observes_running_workspace_until_miss_threshold(self) -> None:
        ws = make_workspace(
            workspace_id="ws-zombie",
            status="running",
            transition="start",
            agent_statuses=["disconnected"],
        )
        self.healer.coder_api.request = MagicMock(return_value=(200, [ws]))  # type: ignore[method-assign]

        # Run 1: miss 1 -> observe
        decisions1 = self.healer.heal_workspaces()
        self.assertEqual(decisions1[0]["action"], "observe")
        self.assertEqual(decisions1[0]["consecutive_misses"], 1)

        # Run 2: miss 2 -> observe
        decisions2 = self.healer.heal_workspaces()
        self.assertEqual(decisions2[0]["action"], "observe")
        self.assertEqual(decisions2[0]["consecutive_misses"], 2)

        # Run 3: miss 3 >= threshold -> restart
        decisions3 = self.healer.heal_workspaces()
        self.assertEqual(decisions3[0]["action"], "restart")
        self.assertEqual(decisions3[0]["consecutive_misses"], 3)

    def test_healer_restarts_failed_start_build_on_first_miss(self) -> None:
        ws = make_workspace(
            workspace_id="ws-failed",
            status="failed",
            transition="start",
            job_error="provisioner timeout",
        )
        self.healer.coder_api.request = MagicMock(return_value=(200, [ws]))  # type: ignore[method-assign]

        decisions = self.healer.heal_workspaces()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action"], "restart")
        self.assertEqual(decisions[0]["workspace_id"], "ws-failed")

    def test_healer_respects_max_heals_per_run_limit(self) -> None:
        workspaces = [
            make_workspace(f"ws-failed-{i}", status="failed", transition="start") for i in range(5)
        ]
        self.healer.coder_api.request = MagicMock(return_value=(200, workspaces))  # type: ignore[method-assign]

        decisions = self.healer.heal_workspaces()

        actions = [d["action"] for d in decisions]
        self.assertEqual(actions.count("restart"), 3)
        self.assertEqual(actions.count("defer"), 2)

    def test_dry_run_does_not_mutate_or_call_restart_api(self) -> None:
        dry_healer = WorkspaceHealer(
            coder_url="https://coder.example.com",
            session_token="test-token",
            dry_run=True,
            rate_limiter=RateLimiter(0.0),
        )
        ws = make_workspace("ws-dry", status="failed", transition="start")
        mock_request = MagicMock(return_value=(200, [ws]))
        dry_healer.coder_api.request = mock_request  # type: ignore[method-assign]

        decisions = dry_healer.heal_workspaces()

        self.assertEqual(decisions[0]["action"], "dry_run_restart")
        # Only GET /api/v2/workspaces was called, no POST /builds
        self.assertEqual(mock_request.call_count, 1)
        self.assertEqual(mock_request.call_args[0][0], "GET")

    def test_healer_records_error_decision_on_restart_failure(self) -> None:
        ws = make_workspace("ws-err", status="failed", transition="start")

        def mock_request(method: str, path: str, **kwargs: object) -> tuple[int, object | None]:
            del kwargs
            if method == "GET":
                return 200, [ws]
            raise BootstrapError("API unavailable")

        self.healer.coder_api.request = MagicMock(side_effect=mock_request)  # type: ignore[method-assign]

        decisions = self.healer.heal_workspaces()

        self.assertEqual(decisions[0]["action"], "error")


class TestStructuredLogging(unittest.TestCase):
    def test_structured_log_records_contractual_fields(self) -> None:
        records: list[str] = []
        custom_logger = logging.getLogger("test_structured_log")
        custom_logger.handlers = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        custom_logger.addHandler(CaptureHandler())
        custom_logger.setLevel(logging.INFO)

        healer = WorkspaceHealer(
            coder_url="https://coder.example.com",
            session_token="test-token",
            rate_limiter=RateLimiter(0.0),
            logger=custom_logger,
        )
        ws = make_workspace(
            workspace_id="ws-log-test",
            owner_name="bob",
            status="failed",
            transition="start",
        )
        healer.coder_api.request = MagicMock(return_value=(200, [ws]))  # type: ignore[method-assign]

        healer.heal_workspaces()

        self.assertEqual(len(records), 1)
        parsed = json.loads(records[0])
        self.assertEqual(parsed["workspace_id"], "ws-log-test")
        self.assertEqual(parsed["owner"], "bob")
        self.assertEqual(parsed["action"], "restart")
        self.assertIn("consecutive_misses", parsed)
        self.assertIn("reason", parsed)


class TestCliEntryPoint(unittest.TestCase):
    def test_cli_fails_without_coder_url(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            exit_code = main(["--token-file", "/dev/null"])
            self.assertEqual(exit_code, 1)

    def test_cli_parses_arguments_correctly(self) -> None:
        args = parse_args([
            "--coder-url",
            "https://coder.local",
            "--token-file",
            "/tmp/token",
            "--dry-run",
            "--max-heals-per-run",
            "5",
            "--consecutive-miss-threshold",
            "4",
        ])
        self.assertEqual(args.coder_url, "https://coder.local")
        self.assertEqual(args.token_file, "/tmp/token")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.max_heals_per_run, 5)
        self.assertEqual(args.consecutive_miss_threshold, 4)

    def test_cli_parses_continuous_arguments(self) -> None:
        args = parse_args([
            "--continuous",
            "--interval-seconds",
            "60",
            "--heartbeat-file",
            "/tmp/test-healer-healthy",
        ])
        self.assertTrue(args.continuous)
        self.assertEqual(args.interval_seconds, 60)
        self.assertEqual(args.heartbeat_file, "/tmp/test-healer-healthy")

    def test_touch_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            heartbeat_file = Path(tmp_dir) / "sub" / "healthy"
            touch_heartbeat(heartbeat_file)
            self.assertTrue(heartbeat_file.is_file())

    def test_run_continuous_loop_terminates_on_stop_event(self) -> None:
        stop_event = threading.Event()
        stop_event.set()

        healer_mock = MagicMock()
        run_continuous_loop(
            healer=healer_mock,
            interval_seconds=1,
            heartbeat_file=None,
            stop_event=stop_event,
        )
        healer_mock.heal_workspaces.assert_not_called()


if __name__ == "__main__":
    unittest.main()
