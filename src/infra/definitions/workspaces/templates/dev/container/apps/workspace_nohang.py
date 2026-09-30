#!/usr/bin/env python3
"""Userspace memory pressure monitor and runaway process freezer for devpod containers.

Monitors cgroup v2 memory consumption and Pressure Stall Information (PSI).
When memory pressure approaches critical limits, it freezes (SIGSTOP) the largest
non-whitelisted process to halt memory allocation without terminating the process or
crashing the container, protecting critical developer infrastructure.
"""

from __future__ import annotations

import argparse
import operator
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROTECTED_PROCESS_PATTERN: re.Pattern[str] = re.compile(
    r"^(coder|coder-agent|code-server|node|sshd|zsh|bash|zellij|paseo|zasper|kopia)$"
)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    comm: str
    rss_bytes: int
    is_stopped: bool


def _read_inactive_file(stat_path: Path, keys: set[str]) -> int:
    """Extracts inactive file page cache bytes from cgroup memory.stat."""
    if not stat_path.exists():
        return 0
    try:
        for line in stat_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in keys:
                return int(parts[1])
    except (OSError, ValueError):
        pass
    return 0


def read_cgroup_memory() -> tuple[int | None, int | None]:
    """Reads cgroup v2 (or v1 fallback) non-reclaimable memory usage and limit in bytes.

    Discounting reclaimable page cache (inactive_file) prevents false positive OOM
    interventions when build tools perform heavy filesystem I/O.
    """
    # cgroup v2 standard paths
    current_path = Path("/sys/fs/cgroup/memory.current")
    max_path = Path("/sys/fs/cgroup/memory.max")
    stat_path = Path("/sys/fs/cgroup/memory.stat")

    if current_path.exists() and max_path.exists():
        try:
            usage = int(current_path.read_text(encoding="utf-8").strip())
            limit_str = max_path.read_text(encoding="utf-8").strip()
            limit = None if limit_str == "max" else int(limit_str)
            usage = max(0, usage - _read_inactive_file(stat_path, {"inactive_file"}))
            return usage, limit
        except (OSError, ValueError):
            pass

    # cgroup v1 fallback
    v1_current = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    v1_max = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    v1_stat = Path("/sys/fs/cgroup/memory/memory.stat")
    if v1_current.exists() and v1_max.exists():
        try:
            usage = int(v1_current.read_text(encoding="utf-8").strip())
            limit = int(v1_max.read_text(encoding="utf-8").strip())
            if limit >= 0x7FFFFFFFFFFFF000:
                limit = None
            usage = max(
                0,
                usage - _read_inactive_file(v1_stat, {"total_inactive_file", "inactive_file"}),
            )
            return usage, limit
        except (OSError, ValueError):
            pass

    return None, None


def read_memory_pressure() -> float | None:
    """Reads PSI some avg10 memory pressure stall metric."""
    for path_str in ("/sys/fs/cgroup/memory.pressure", "/proc/pressure/memory"):
        p = Path(path_str)
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.startswith(("some ", "full ")):
                        for part in line.split():
                            if part.startswith("avg10="):
                                return float(part.split("=")[1])
            except (OSError, ValueError):
                pass
    return None


def get_process_info(pid: int) -> ProcessInfo | None:
    """Returns memory usage and comm name for a given PID."""
    try:
        comm_path = Path(f"/proc/{pid}/comm")
        statm_path = Path(f"/proc/{pid}/statm")
        status_path = Path(f"/proc/{pid}/status")

        comm = comm_path.read_text(encoding="utf-8").strip()

        is_stopped = False
        if status_path.exists():
            for line in status_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("State:") and (
                    "T (stopped)" in line or line.startswith("State:\tT")
                ):
                    is_stopped = True
                    break

        fields = statm_path.read_text(encoding="utf-8").split()
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        rss_bytes = resident_pages * page_size

        return ProcessInfo(
            pid=pid,
            comm=comm,
            rss_bytes=rss_bytes,
            is_stopped=is_stopped,
        )
    except (OSError, IndexError, ValueError):
        return None


def find_victim_process(
    protected_pattern: re.Pattern[str] = PROTECTED_PROCESS_PATTERN,
) -> ProcessInfo | None:
    """Identifies the non-whitelisted process with the highest RSS."""
    my_pid = os.getpid()
    candidates: list[ProcessInfo] = []

    pids: list[int] = []
    cgroup_procs_path = Path("/sys/fs/cgroup/cgroup.procs")
    if cgroup_procs_path.exists():
        try:
            for line in cgroup_procs_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.isdigit():
                    pids.append(int(stripped))
        except OSError:
            pids = []

    if not pids:
        try:
            pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
        except OSError:
            pids = []

    for pid in pids:
        if pid <= 1 or pid == my_pid:
            continue
        info = get_process_info(pid)
        if not info:
            continue
        if info.is_stopped:
            continue
        if protected_pattern.search(info.comm):
            continue
        candidates.append(info)

    if not candidates:
        return None

    candidates.sort(key=operator.attrgetter("rss_bytes"), reverse=True)
    return candidates[0]


def check_and_freeze(
    *,
    threshold_percent: float = 90.0,
    headroom_mb: float = 512.0,
    psi_threshold: float = 40.0,
    dry_run: bool = False,
) -> bool:
    """Checks memory pressure and freezes runaway processes if critical."""
    usage, limit = read_cgroup_memory()
    psi_avg10 = read_memory_pressure()

    pressure_detected = False
    details: list[str] = []

    if usage is not None and limit is not None and limit > 0:
        pct = (usage / limit) * 100.0
        remaining_mb = (limit - usage) / (1024 * 1024)
        if pct >= threshold_percent or remaining_mb <= headroom_mb:
            pressure_detected = True
            details.append(
                f"usage={usage // (1024 * 1024)}MiB / {limit // (1024 * 1024)}MiB ({pct:.1f}%)"
            )

    if psi_avg10 is not None and psi_avg10 >= psi_threshold:
        pressure_detected = True
        details.append(f"PSI avg10={psi_avg10:.1f}")

    if not pressure_detected:
        return False

    victim = find_victim_process()
    if not victim:
        return False

    pid = victim.pid
    comm = victim.comm
    rss_mb = victim.rss_bytes / (1024 * 1024)

    msg = (
        f"[nohang] Memory pressure critical ({', '.join(details)}). "
        f"Freezing process PID {pid} ({comm}, RSS {rss_mb:.1f} MiB) with SIGSTOP to prevent OOM crash.\n"
        f"[nohang] To resume the process: kill -CONT {pid}\n"
        f"[nohang] To terminate the process: kill -9 {pid}"
    )
    print(msg, file=sys.stderr, flush=True)

    if not dry_run:
        try:
            os.kill(pid, signal.SIGSTOP)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as e:
            print(
                f"[nohang] Permission denied freezing PID {pid}: {e}",
                file=sys.stderr,
                flush=True,
            )
            return False
    return True


def run_daemon(
    *,
    interval_seconds: float = 1.0,
    threshold_percent: float = 90.0,
    headroom_mb: float = 512.0,
    psi_threshold: float = 40.0,
    cooldown_seconds: float = 5.0,
    dry_run: bool = False,
) -> None:
    """Runs the memory monitoring daemon loop."""
    running = True

    def _handle_signal(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(
        f"[nohang] Workspace OOM guard daemon started (threshold={threshold_percent}%, psi={psi_threshold}).",
        file=sys.stderr,
        flush=True,
    )

    last_action_time = 0.0
    while running:
        now = time.time()
        if (now - last_action_time >= cooldown_seconds) and check_and_freeze(
            threshold_percent=threshold_percent,
            headroom_mb=headroom_mb,
            psi_threshold=psi_threshold,
            dry_run=dry_run,
        ):
            last_action_time = time.time()
        time.sleep(interval_seconds)

    print("[nohang] Workspace OOM guard daemon stopped.", file=sys.stderr, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Container memory pressure monitor and process freezer daemon."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--threshold-percent",
        type=float,
        default=90.0,
        help="Memory usage percentage to trigger freeze (default: 90.0)",
    )
    parser.add_argument(
        "--headroom-mb",
        type=float,
        default=512.0,
        help="Remaining memory headroom in MiB to trigger freeze (default: 512.0)",
    )
    parser.add_argument(
        "--psi-threshold",
        type=float,
        default=40.0,
        help="PSI some avg10 threshold to trigger freeze (default: 40.0)",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=5.0,
        help="Cooldown between actions in seconds (default: 5.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate without sending SIGSTOP",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit",
    )

    args = parser.parse_args()

    if args.once:
        check_and_freeze(
            threshold_percent=args.threshold_percent,
            headroom_mb=args.headroom_mb,
            psi_threshold=args.psi_threshold,
            dry_run=args.dry_run,
        )
    else:
        run_daemon(
            interval_seconds=args.interval,
            threshold_percent=args.threshold_percent,
            headroom_mb=args.headroom_mb,
            psi_threshold=args.psi_threshold,
            cooldown_seconds=args.cooldown,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
