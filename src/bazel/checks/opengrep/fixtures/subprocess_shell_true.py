"""Intentional Semgrep regression fixture for prohibited source patterns."""

import subprocess


def known_unsafe_pattern(command: str = "/opt/fixture/tool.py") -> None:
    """Exercise the blocking rule; this call must remain intentionally unsafe."""
    subprocess.run(command, shell=True, check=True)
