#!/usr/bin/env python3
"""Validate local cluster network topology, subnet CIDR allocations, and host port ranges across components."""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

import yaml

IPV4_VERSION = 4


class ClusterNetworkError(ValueError):
    """Cluster network configuration violates its repository contract."""


def validate_network_ips(
    clusters_path: Path,
    network_cfg: dict[str, Any],
    selections: list[tuple[str, dict[str, Any]]],
) -> None:
    try:
        subnet = ipaddress.ip_network(network_cfg["subnet"], strict=True)
        dynamic_range = ipaddress.ip_network(network_cfg["dynamic_range"], strict=True)
    except (KeyError, ValueError) as error:
        raise ClusterNetworkError(
            f"{clusters_path}: invalid local IPv4 subnet/dynamic_range: {error}"
        ) from error

    if (
        subnet.version != IPV4_VERSION
        or dynamic_range.version != IPV4_VERSION
        or not dynamic_range.subnet_of(subnet)
    ):
        raise ClusterNetworkError(
            f"{clusters_path}: local dynamic_range must be inside the IPv4 subnet"
        )

    try:
        node_addresses = [ipaddress.ip_address(s["node_ipv4"]) for _, s in selections]
        service_addresses = [
            ipaddress.ip_address(addr) for addr in network_cfg.get("services", {}).values()
        ]
    except (KeyError, ValueError) as error:
        raise ClusterNetworkError(
            f"{clusters_path}: invalid IP address in network config: {error}"
        ) from error

    addresses = [*node_addresses, *service_addresses]
    if len(addresses) != len(set(addresses)):
        raise ClusterNetworkError(
            f"{clusters_path}: local node and service IPv4 addresses must be distinct"
        )

    for addr in addresses:
        if addr not in subnet:
            raise ClusterNetworkError(f"{clusters_path}: local address {addr} is outside {subnet}")
        if addr in dynamic_range:
            raise ClusterNetworkError(
                f"{clusters_path}: local address {addr} overlaps dynamic_range {dynamic_range}"
            )
        if addr in {subnet.network_address, subnet.broadcast_address}:
            raise ClusterNetworkError(
                f"{clusters_path}: local address {addr} is reserved by {subnet}"
            )


def _record_port_allocations(
    allocated_ports: dict[int, str],
    name: str,
    assignments: Mapping[str, Sequence[object]],
    clusters_path: Path,
) -> None:
    for service, ports in assignments.items():
        for port in ports:
            if not isinstance(port, int):
                continue
            owner = allocated_ports.get(port)
            if owner is not None:
                raise ClusterNetworkError(
                    f"{clusters_path}: host port {port} overlaps {owner} and {name} {service}"
                )
            allocated_ports[port] = f"{name} {service}"


def validate_host_ports(
    clusters_path: Path,
    selections: list[tuple[str, dict[str, Any]]],
) -> None:
    allocated_ports: dict[int, str] = {}
    for _, selection in selections:
        name = selection.get("record", "unknown")
        host_ports = selection.get("host_ports", {})
        eks_ports = host_ports.get("eks_api_server", {})
        eks_base = eks_ports.get("base", 0)
        eks_max = eks_ports.get("max", 0)
        if eks_base > eks_max:
            raise ClusterNetworkError(
                f"{clusters_path}: {name} Kubernetes API host-port range is inverted"
            )

        assignments = {
            "AWS API": [host_ports.get("aws_api")],
            "ECR registry": [host_ports.get("ecr_registry")],
            "Kubernetes API": list(range(eks_base, eks_max + 1)),
        }
        _record_port_allocations(allocated_ports, name, assignments, clusters_path)


def validate_cluster_network(clusters_path: Path) -> None:
    content = yaml.safe_load(clusters_path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ClusterNetworkError(f"{clusters_path}: expected dictionary root")

    if "local" in content:
        local = content["local"]
        network_cfg = local.get("network", {})
        selections: list[tuple[str, dict[str, Any]]] = [
            ("ctrl", local.get("control", {})),
            *[("cell", cell) for cell in local.get("cells", [])],
        ]
    elif "network" in content and "clusters" in content:
        network_cfg = content.get("network", {})
        selections = []
        for cluster in content.get("clusters", {}).values():
            role = cluster.get("role", "cell")
            net = cluster.get("network", {})
            selections.append((
                role,
                {
                    "record": cluster.get("name"),
                    "node_ipv4": net.get("node_ipv4"),
                    "host_ports": net.get("host_ports", {}),
                },
            ))
    else:
        raise ClusterNetworkError(
            f"{clusters_path}: expected 'local' or self-contained deployment configuration"
        )

    names = [s.get("record") for _, s in selections]
    if len(names) != len(set(names)):
        raise ClusterNetworkError(f"{clusters_path}: local cluster records must be distinct")

    validate_network_ips(clusters_path, network_cfg, selections)
    validate_host_ports(clusters_path, selections)


def main() -> int:
    default_path = (
        Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd()))
        / "src/infra/terraform/deployments/local/deployment.yaml"
    )
    if not default_path.is_file():
        default_path = (
            Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd()))
            / "src/infra/terraform/deployments/local/deployment.yaml"
        )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--clusters",
        type=Path,
        default=default_path,
    )
    args = parser.parse_args()
    try:
        validate_cluster_network(args.clusters.resolve())
    except (ClusterNetworkError, OSError, yaml.YAMLError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
