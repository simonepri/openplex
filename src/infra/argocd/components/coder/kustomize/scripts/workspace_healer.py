#!/usr/bin/env python3
"""Reconcile and heal diverged or zombie Coder workspaces."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import stat
import sys
import time
from itertools import starmap
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from coder_api import BootstrapError, CoderAPI

if TYPE_CHECKING:
    import threading
    from collections.abc import Sequence
    from types import FrameType

OPT_OUT_ANNOTATION = "healer.coder.openplex.dev/opt-out"
TRUTHY_VALUES = frozenset({"true", "1", "yes", "on", "enabled"})
TERMINAL_STATUSES = frozenset({"running", "failed"})
UNHEALTHY_AGENT_STATUSES = frozenset({"disconnected", "timeout"})


class Decision(TypedDict):
    workspace_id: str
    owner: str
    consecutive_misses: int
    action: str
    reason: str


def default_logger() -> logging.Logger:
    logger = logging.getLogger("workspace_healer")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def read_token_safely(token_file: Path | str, *, unlink: bool = False) -> str:
    token_path = Path(token_file)
    try:
        path = token_path.resolve()
        fd = os.open(path, os.O_RDONLY)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Cannot open token file: {token_file}") from error

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"Token file must be a regular file: {token_file}")
        chunks: list[bytes] = []
        total_bytes = 0
        while chunk := os.read(fd, 4096):
            total_bytes += len(chunk)
            if total_bytes > 1024 * 1024:
                raise ValueError(f"Token file exceeds maximum allowed size: {token_file}")
            chunks.append(chunk)
        token = b"".join(chunks).decode("utf-8").strip()
    finally:
        os.close(fd)

    if not token:
        raise ValueError(f"Token file is empty: {token_file}")

    if unlink:
        try:
            token_path.unlink()
        except OSError as error:
            raise ValueError(f"Failed to unlink token file: {token_file}") from error

    return token


def _is_truthy_opt_out(key: object, val: object) -> bool:
    k_norm = str(key).strip().lower()
    v_norm = str(val).strip().lower()
    matches_key = k_norm == OPT_OUT_ANNOTATION or ("opt-out" in k_norm and "healer" in k_norm)
    return matches_key and v_norm in TRUTHY_VALUES


def _has_opt_out_in_params(params: object) -> bool:
    if not isinstance(params, list):
        return False
    return any(
        _is_truthy_opt_out(p.get("name", p.get("key", "")), p.get("value", ""))
        for p in params
        if isinstance(p, dict)
    )


def is_opted_out(workspace: dict[str, Any]) -> bool:
    for field in ("annotations", "labels", "tags"):
        mapping = workspace.get(field)
        if isinstance(mapping, dict) and any(starmap(_is_truthy_opt_out, mapping.items())):
            return True

    for param_field in ("parameters", "template_version_parameters"):
        if _has_opt_out_in_params(workspace.get(param_field)):
            return True

    latest_build = workspace.get("latest_build")
    if isinstance(latest_build, dict):
        if _has_opt_out_in_params(
            latest_build.get("parameters") or latest_build.get("build_parameters")
        ):
            return True
        for resource in latest_build.get("resources") or []:
            if isinstance(resource, dict) and _has_opt_out_in_params(resource.get("metadata")):
                return True

    return False


def get_workspace_owner(workspace: dict[str, Any]) -> str:
    if owner_name := workspace.get("owner_name"):
        return str(owner_name)
    if owner_user := workspace.get("owner_username"):
        return str(owner_user)
    owner_obj = workspace.get("owner")
    if isinstance(owner_obj, dict) and (username := owner_obj.get("username")):
        return str(username)
    if owner_id := workspace.get("owner_id"):
        return str(owner_id)
    return "unknown"


def _extract_agents_from_resources(resources: object) -> list[dict[str, Any]]:
    if not isinstance(resources, list):
        return []
    result: list[dict[str, Any]] = []
    for resource in resources:
        if isinstance(resource, dict):
            for agent in resource.get("agents") or []:
                if isinstance(agent, dict):
                    result.append(agent)
    return result


def get_workspace_agents(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    latest_build = workspace.get("latest_build")
    if isinstance(latest_build, dict):
        candidates.extend(_extract_agents_from_resources(latest_build.get("resources")))
        candidates.extend([a for a in latest_build.get("agents") or [] if isinstance(a, dict)])
    candidates.extend(_extract_agents_from_resources(workspace.get("resources")))
    candidates.extend([a for a in workspace.get("agents") or [] if isinstance(a, dict)])

    unique_agents: list[dict[str, Any]] = []
    for agent in candidates:
        if agent not in unique_agents:
            unique_agents.append(agent)
    return unique_agents


class RateLimiter:
    """Limits request rate using minimum call intervals."""

    def __init__(self, requests_per_second: float = 5.0) -> None:
        self.interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.last_call = 0.0

    def acquire(self) -> None:
        if self.interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.monotonic()


class StateTracker:
    """Tracks consecutive misses for workspaces across healer executions."""

    def __init__(self, state_file: Path | str | None = None) -> None:
        self.state_file = Path(state_file) if state_file else None
        self._misses: dict[str, int] = {}
        self.load()

    def get_misses(self, workspace_id: str) -> int:
        return self._misses.get(workspace_id, 0)

    def record_miss(self, workspace_id: str) -> int:
        count = self._misses.get(workspace_id, 0) + 1
        self._misses[workspace_id] = count
        return count

    def record_healthy(self, workspace_id: str) -> None:
        self._misses.pop(workspace_id, None)

    def prune(self, active_workspace_ids: set[str]) -> None:
        stale = [wid for wid in self._misses if wid not in active_workspace_ids]
        for wid in stale:
            del self._misses[wid]

    def load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            content = self.state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict):
                self._misses = {
                    str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))
                }
        except (OSError, json.JSONDecodeError, ValueError):
            self._misses = {}

    def save(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_name(f"{self.state_file.name}.{os.getpid()}.tmp")
            tmp_file.write_text(json.dumps(self._misses, indent=2), encoding="utf-8")
            tmp_file.replace(self.state_file)
        except OSError:
            pass


class WorkspaceHealer:
    """Reconciles Coder workspaces and heals zombie or diverged instances."""

    def __init__(
        self,
        coder_url: str,
        session_token: str,
        *,
        dry_run: bool = False,
        rate_limiter: RateLimiter | float | None = None,
        max_heals_per_run: int = 3,
        consecutive_miss_threshold: int = 3,
        failed_miss_threshold: int = 1,
        state_tracker: StateTracker | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.coder_url = coder_url.rstrip("/")
        self.session_token = session_token
        self.dry_run = dry_run
        self.max_heals_per_run = max_heals_per_run
        self.consecutive_miss_threshold = consecutive_miss_threshold
        self.failed_miss_threshold = failed_miss_threshold
        self.coder_api = CoderAPI(self.coder_url, self.session_token)
        self.state_tracker = state_tracker or StateTracker()
        self.logger = logger or default_logger()

        if isinstance(rate_limiter, (int, float)):
            self.rate_limiter = RateLimiter(float(rate_limiter))
        elif rate_limiter is None:
            self.rate_limiter = RateLimiter(5.0)
        else:
            self.rate_limiter = rate_limiter

    def log_decision(
        self,
        workspace_id: str,
        owner: str,
        consecutive_misses: int,
        action: str,
        reason: str,
    ) -> Decision:
        decision: Decision = {
            "workspace_id": workspace_id,
            "owner": owner,
            "consecutive_misses": consecutive_misses,
            "action": action,
            "reason": reason,
        }
        self.logger.info(json.dumps(decision, separators=(",", ":")))
        return decision

    def fetch_workspaces(self) -> list[dict[str, Any]]:
        _, body = self.coder_api.request("GET", "/api/v2/workspaces", accepted={200})
        if isinstance(body, dict):
            workspaces = body.get("workspaces")
            if isinstance(workspaces, list):
                return [w for w in workspaces if isinstance(w, dict)]
        elif isinstance(body, list):
            return [w for w in body if isinstance(w, dict)]
        return []

    def restart_workspace(self, workspace_id: str) -> None:
        if hasattr(self.rate_limiter, "acquire"):
            self.rate_limiter.acquire()
        elif callable(self.rate_limiter):
            self.rate_limiter()
        payload = {"transition": "start"}
        self.coder_api.request(
            "POST",
            f"/api/v2/workspaces/{workspace_id}/builds",
            payload=payload,
            accepted={200, 201},
        )

    def _evaluate_running_workspace(
        self,
        workspace: dict[str, Any],
        workspace_id: str,
        owner: str,
    ) -> tuple[Decision | None, str | None]:
        agents = get_workspace_agents(workspace)
        if not agents:
            self.state_tracker.record_healthy(workspace_id)
            return (
                self.log_decision(
                    workspace_id, owner, 0, "skip", "Running workspace has no agents"
                ),
                None,
            )

        all_unhealthy = all(
            str(agent.get("status") or "").lower() in UNHEALTHY_AGENT_STATUSES for agent in agents
        )
        if not all_unhealthy:
            self.state_tracker.record_healthy(workspace_id)
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    0,
                    "skip",
                    "Workspace is healthy (agents connected or connecting)",
                ),
                None,
            )

        misses = self.state_tracker.record_miss(workspace_id)
        if misses < self.consecutive_miss_threshold:
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    misses,
                    "observe",
                    f"All agents disconnected/timed out: consecutive misses ({misses}) < threshold ({self.consecutive_miss_threshold})",
                ),
                None,
            )

        reason = (
            f"Running workspace with all agents disconnected/timed out "
            f"(consecutive misses: {misses} >= {self.consecutive_miss_threshold})"
        )
        return None, reason

    def _evaluate_failed_workspace(
        self,
        workspace_id: str,
        owner: str,
    ) -> tuple[Decision | None, str | None]:
        misses = self.state_tracker.record_miss(workspace_id)
        if misses < self.failed_miss_threshold:
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    misses,
                    "observe",
                    f"Start build failed: consecutive misses ({misses}) < threshold ({self.failed_miss_threshold})",
                ),
                None,
            )

        reason = (
            f"Start build failed after reaper timeout or error "
            f"(consecutive misses: {misses} >= {self.failed_miss_threshold})"
        )
        return None, reason

    def _validate_workspace_build(
        self, workspace: dict[str, Any], workspace_id: str, owner: str
    ) -> tuple[Decision | None, str | None]:
        if is_opted_out(workspace):
            self.state_tracker.record_healthy(workspace_id)
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    0,
                    "skip",
                    f"Workspace opted out via {OPT_OUT_ANNOTATION}",
                ),
                None,
            )

        latest_build = workspace.get("latest_build")
        if not isinstance(latest_build, dict):
            self.state_tracker.record_healthy(workspace_id)
            return (
                self.log_decision(workspace_id, owner, 0, "skip", "Workspace has no latest build"),
                None,
            )

        transition = str(latest_build.get("transition") or "")
        if transition != "start":
            self.state_tracker.record_healthy(workspace_id)
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    0,
                    "skip",
                    f"Latest build transition is '{transition}' (not start)",
                ),
                None,
            )

        status = str(latest_build.get("status") or "")
        if status not in TERMINAL_STATUSES:
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    self.state_tracker.get_misses(workspace_id),
                    "skip",
                    f"Latest build status is '{status}' (in progress/non-terminal)",
                ),
                None,
            )

        return None, status

    def _execute_heal(
        self, workspace_id: str, owner: str, heal_reason: str
    ) -> tuple[Decision, bool]:
        current_misses = self.state_tracker.get_misses(workspace_id)
        if self.dry_run:
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    current_misses,
                    "dry_run_restart",
                    heal_reason,
                ),
                True,
            )

        try:
            self.restart_workspace(workspace_id)
            self.state_tracker.record_healthy(workspace_id)
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    current_misses,
                    "restart",
                    heal_reason,
                ),
                True,
            )
        except (BootstrapError, OSError) as error:
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    current_misses,
                    "error",
                    f"Failed to restart workspace: {error}",
                ),
                False,
            )

    def evaluate_workspace(
        self, workspace: dict[str, Any], heals_so_far: int
    ) -> tuple[Decision, bool]:
        workspace_id = str(workspace.get("id") or "")
        owner = get_workspace_owner(workspace)

        if not workspace_id:
            return (
                self.log_decision("", owner, 0, "skip", "Workspace missing identifier"),
                False,
            )

        validation_decision, status = self._validate_workspace_build(workspace, workspace_id, owner)
        if validation_decision is not None:
            return validation_decision, False

        assert status is not None
        if status == "running":
            terminal_decision, heal_reason = self._evaluate_running_workspace(
                workspace, workspace_id, owner
            )
        else:
            terminal_decision, heal_reason = self._evaluate_failed_workspace(workspace_id, owner)

        if terminal_decision is not None:
            return terminal_decision, False

        assert heal_reason is not None
        current_misses = self.state_tracker.get_misses(workspace_id)
        if heals_so_far >= self.max_heals_per_run:
            return (
                self.log_decision(
                    workspace_id,
                    owner,
                    current_misses,
                    "defer",
                    f"Max heals limit reached ({heals_so_far}/{self.max_heals_per_run})",
                ),
                False,
            )

        return self._execute_heal(workspace_id, owner, heal_reason)

    def heal_workspaces(self) -> list[Decision]:
        workspaces = self.fetch_workspaces()
        active_ids = {str(w["id"]) for w in workspaces if "id" in w}
        self.state_tracker.prune(active_ids)

        decisions: list[Decision] = []
        heals_count = 0
        for workspace in workspaces:
            decision, did_heal = self.evaluate_workspace(workspace, heals_count)
            decisions.append(decision)
            if did_heal:
                heals_count += 1

        if not self.dry_run:
            self.state_tracker.save()

        return decisions


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect and heal zombie or diverged Coder workspaces."
    )
    parser.add_argument(
        "--coder-url",
        default=os.environ.get("CODER_URL", ""),
        help="Coder API base URL (defaults to CODER_URL env var)",
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("CODER_SESSION_TOKEN_FILE", ""),
        help="Path to session token file (defaults to CODER_SESSION_TOKEN_FILE env var)",
    )
    parser.add_argument(
        "--unlink-token",
        action="store_true",
        default=False,
        help="Unlink token file after reading it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log healing decisions without restarting workspaces",
    )
    parser.add_argument(
        "--max-heals-per-run",
        type=int,
        default=3,
        help="Maximum number of workspaces to heal per run (default: 3)",
    )
    parser.add_argument(
        "--consecutive-miss-threshold",
        type=int,
        default=3,
        help="Consecutive misses required before healing running workspaces (default: 3)",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("CODER_HEALER_STATE_FILE", ""),
        help="Path to state file for tracking consecutive misses across runs",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=5.0,
        help="Maximum requests per second to Coder API (default: 5.0)",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        default=False,
        help="Run reconciliation loop continuously at fixed intervals",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=120,
        help="Interval between reconciliation cycles in continuous mode (default: 120)",
    )
    parser.add_argument(
        "--heartbeat-file",
        default=os.environ.get("CODER_HEALER_HEARTBEAT_FILE", ""),
        help="Path to heartbeat file touched after each successful reconciliation cycle",
    )
    return parser.parse_args(argv)


def touch_heartbeat(heartbeat_file: str | Path | None) -> None:
    if not heartbeat_file:
        return
    path = Path(heartbeat_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


class LoopControl:
    """Controls the continuous reconciliation loop lifecycle."""

    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.running: bool = True
        self.stop_event = stop_event

    def should_continue(self) -> bool:
        if self.stop_event is not None and self.stop_event.is_set():
            return False
        return self.running

    def stop(self) -> None:
        self.running = False


def run_continuous_loop(
    healer: WorkspaceHealer,
    interval_seconds: int,
    heartbeat_file: str | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    control = LoopControl(stop_event=stop_event)

    def handle_signal(_signum: int, _frame: FrameType | None) -> None:
        control.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while control.should_continue():
        try:
            healer.heal_workspaces()
            touch_heartbeat(heartbeat_file)
        except Exception as error:
            sys.stderr.write(f"Error executing workspace healer cycle: {error}\n")

        end_time = time.monotonic() + max(1, interval_seconds)
        while control.should_continue() and time.monotonic() < end_time:
            time.sleep(min(1.0, max(0.1, end_time - time.monotonic())))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.coder_url:
        sys.stderr.write("Error: --coder-url or CODER_URL environment variable is required\n")
        return 1

    session_token = ""
    if args.token_file:
        try:
            session_token = read_token_safely(args.token_file, unlink=args.unlink_token)
        except (ValueError, OSError) as error:
            sys.stderr.write(f"Error reading token file: {error}\n")
            return 1
    elif os.environ.get("CODER_SESSION_TOKEN"):
        session_token = os.environ["CODER_SESSION_TOKEN"]
    else:
        sys.stderr.write(
            "Error: --token-file, CODER_SESSION_TOKEN_FILE, or CODER_SESSION_TOKEN is required\n"
        )
        return 1

    state_tracker = StateTracker(args.state_file) if args.state_file else None
    rate_limiter = RateLimiter(args.rate_limit)

    healer = WorkspaceHealer(
        coder_url=args.coder_url,
        session_token=session_token,
        dry_run=args.dry_run,
        rate_limiter=rate_limiter,
        max_heals_per_run=args.max_heals_per_run,
        consecutive_miss_threshold=args.consecutive_miss_threshold,
        state_tracker=state_tracker,
    )

    if args.continuous:
        run_continuous_loop(
            healer=healer,
            interval_seconds=args.interval_seconds,
            heartbeat_file=args.heartbeat_file or None,
        )
        return 0

    try:
        healer.heal_workspaces()
        touch_heartbeat(args.heartbeat_file or None)
    except Exception as error:
        sys.stderr.write(f"Error executing workspace healer: {error}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
