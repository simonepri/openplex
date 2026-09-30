#!/usr/bin/env python3
"""Validates cgroup metric parsing, whitelist filters, and process victim selection in workspace-nohang.

Tests that critical developer infrastructure processes are protected from SIGSTOP interventions.
"""

from __future__ import annotations

import math
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import workspace_nohang


class WorkspaceNohangTest(unittest.TestCase):
    def test_protected_processes_are_never_selected_as_victims(self) -> None:
        protected_names = [
            "coder",
            "coder-agent",
            "code-server",
            "node",
            "sshd",
            "zsh",
            "bash",
            "zellij",
            "paseo",
            "zasper",
            "kopia",
        ]
        for name in protected_names:
            assert workspace_nohang.PROTECTED_PROCESS_PATTERN.search(name) is not None, (
                f"Expected {name} to be protected by whitelist regex"
            )

    def test_unprotected_heavy_process_is_selected_over_light_processes(
        self,
    ) -> None:
        mock_candidates = {
            100: workspace_nohang.ProcessInfo(
                pid=100,
                comm="rustc",
                rss_bytes=1000 * 1024 * 1024,
                is_stopped=False,
            ),
            101: workspace_nohang.ProcessInfo(
                pid=101,
                comm="python3",
                rss_bytes=2000 * 1024 * 1024,
                is_stopped=False,
            ),
            102: workspace_nohang.ProcessInfo(
                pid=102,
                comm="code-server",
                rss_bytes=3000 * 1024 * 1024,
                is_stopped=False,
            ),
        }

        with (
            mock.patch(
                "pathlib.Path.iterdir",
                return_value=[
                    pathlib.Path("/proc/100"),
                    pathlib.Path("/proc/101"),
                    pathlib.Path("/proc/102"),
                ],
            ),
            mock.patch.object(
                workspace_nohang,
                "get_process_info",
                side_effect=mock_candidates.get,
            ),
            mock.patch("pathlib.Path.exists", return_value=False),
            mock.patch("os.getpid", return_value=999),
        ):
            victim = workspace_nohang.find_victim_process()
            assert victim is not None
            assert victim.pid == 101, f"Expected PID 101 (python3, 2000MB) but got {victim}"

    def test_already_stopped_processes_are_not_reselected(self) -> None:
        mock_candidates = {
            200: workspace_nohang.ProcessInfo(
                pid=200,
                comm="stress",
                rss_bytes=4000 * 1024 * 1024,
                is_stopped=True,
            ),
            201: workspace_nohang.ProcessInfo(
                pid=201,
                comm="cargo",
                rss_bytes=1500 * 1024 * 1024,
                is_stopped=False,
            ),
        }

        with (
            mock.patch(
                "pathlib.Path.iterdir",
                return_value=[
                    pathlib.Path("/proc/200"),
                    pathlib.Path("/proc/201"),
                ],
            ),
            mock.patch.object(
                workspace_nohang,
                "get_process_info",
                side_effect=mock_candidates.get,
            ),
            mock.patch("pathlib.Path.exists", return_value=False),
            mock.patch("os.getpid", return_value=999),
        ):
            victim = workspace_nohang.find_victim_process()
            assert victim is not None
            assert victim.pid == 201, f"Expected active PID 201 over stopped PID 200, got {victim}"

    def test_cgroup_memory_parsing_handles_max_and_numeric_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = pathlib.Path(tmpdir)
            current_file = tmppath / "memory.current"
            max_file = tmppath / "memory.max"
            stat_file = tmppath / "memory.stat"

            current_file.write_text("1048576000\n", encoding="utf-8")
            max_file.write_text("2147483648\n", encoding="utf-8")
            stat_file.write_text(
                "anon 200000000\nfile 800000000\ninactive_file 500000000\nactive_file 300000000\n",
                encoding="utf-8",
            )

            def mock_path(path_str: str) -> pathlib.Path:
                if path_str == "/sys/fs/cgroup/memory.current":
                    return current_file
                if path_str == "/sys/fs/cgroup/memory.max":
                    return max_file
                if path_str == "/sys/fs/cgroup/memory.stat":
                    return stat_file
                return pathlib.Path(path_str)

            with mock.patch("workspace_nohang.Path", side_effect=mock_path):
                usage, limit = workspace_nohang.read_cgroup_memory()
                assert limit == 2147483648
                # 1048576000 usage - 500000000 inactive_file = 548576000
                assert usage == 548576000, (
                    f"Expected usage 548576000 after discounting inactive_file, got {usage}"
                )

    def test_psi_parsing_extracts_avg10_correctly(self) -> None:
        psi_content = (
            "some avg10=35.50 avg60=22.10 avg300=10.05 total=1234567\n"
            "full avg10=15.20 avg60=8.00 avg300=2.00 total=7654321\n"
        )
        with (
            mock.patch("pathlib.Path.exists", return_value=True),
            mock.patch("pathlib.Path.read_text", return_value=psi_content),
        ):
            pressure = workspace_nohang.read_memory_pressure()
            assert pressure is not None
            assert math.isclose(pressure, 35.50), f"Expected 35.50, got {pressure}"


if __name__ == "__main__":
    unittest.main()
