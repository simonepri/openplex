#!/usr/bin/env python3
"""Resolves workspace aliases before opening isolated Tailnet SSH streams."""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

TAILNET_IPV4 = ipaddress.ip_network("100.64.0.0/10")
CLUSTER_IPV4 = ipaddress.ip_network("172.16.0.0/12")
ALLOWED_IPV4_NETWORKS = (TAILNET_IPV4, CLUSTER_IPV4)


def resolve_tailnet_ipv4(tailscale: str, socket_path: Path, hostname: str) -> str:
    """Resolve exactly one tailnet IPv4 address for a workspace alias."""
    try:
        result = subprocess.run(
            [
                tailscale,
                f"--socket={socket_path}",
                "dns",
                "query",
                "--json",
                hostname,
                "A",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"tailnet DNS query failed for {hostname}: {error}") from error

    try:
        document: object = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"tailnet DNS returned an invalid response for {hostname}") from error
    if not isinstance(document, dict):
        raise RuntimeError(f"tailnet DNS returned an invalid response for {hostname}")
    if document.get("ResponseCode") != "RCodeSuccess":
        raise RuntimeError(f"tailnet DNS could not resolve {hostname}")

    raw_answers = document.get("Answers")
    if not isinstance(raw_answers, list):
        raise RuntimeError(f"tailnet DNS must return exactly one IPv4 address for {hostname}")
    answers = [
        answer.get("Body")
        for answer in raw_answers
        if isinstance(answer, dict) and answer.get("Type") == "TypeA"
    ]
    if len(answers) != 1 or not isinstance(answers[0], str):
        raise RuntimeError(f"tailnet DNS must return exactly one IPv4 address for {hostname}")

    try:
        address = ipaddress.ip_address(answers[0])
    except ValueError as error:
        raise RuntimeError(
            f"tailnet DNS returned an invalid IPv4 address for {hostname}"
        ) from error
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in network for network in ALLOWED_IPV4_NETWORKS
    ):
        networks_str = ", ".join(str(n) for n in ALLOWED_IPV4_NETWORKS)
        raise RuntimeError(f"tailnet DNS returned an address outside {networks_str} for {hostname}")
    return str(address)


def port_number(value: str) -> int:
    """Parse a TCP port accepted by OpenSSH's percent-p expansion."""
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def connect_tailnet(tailscale: str, socket_path: Path, address: str, port: int) -> int:
    """Connect standard input and output to a tailnet TCP stream."""
    return subprocess.run(
        [
            tailscale,
            f"--socket={socket_path}",
            "nc",
            address,
            str(port),
        ],
        check=False,
    ).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tailscale", required=True)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("hostname")
    parser.add_argument("port", type=port_number)
    args = parser.parse_args(argv)

    try:
        address = resolve_tailnet_ipv4(args.tailscale, args.socket, args.hostname)
        return connect_tailnet(args.tailscale, args.socket, address, args.port)
    except (OSError, RuntimeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
