#!/usr/bin/env python3
"""Test cluster network subnet non-overlap validation and host port allocation conflict detection."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from collections.abc import Iterator

import yaml
from check_cluster_network import ClusterNetworkError, validate_cluster_network

OVERLAPPING_AWS_API_PORT = 4566


@contextmanager
def _assert_raises(expected_type: type[BaseException]) -> Iterator[None]:
    try:
        yield
    except expected_type:
        pass
    else:
        msg = f"Expected {expected_type.__name__} but no exception was raised"
        raise AssertionError(msg)


class ClusterNetworkTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.clusters_path = Path(self.tempdir.name) / "clusters.yaml"
        self.config: dict[str, Any] = {
            "local": {
                "network": {
                    "subnet": "172.19.0.0/16",
                    "dynamic_range": "172.19.0.0/24",
                    "services": {
                        "gateway": "172.19.255.20",
                        "git": "172.19.255.21",
                        "origin_registry": "172.19.255.22",
                    },
                },
                "control": {
                    "record": "ctrl-eaws-lh1",
                    "node_ipv4": "172.19.255.10",
                    "host_ports": {
                        "aws_api": 4566,
                        "ecr_registry": 15100,
                        "eks_api_server": {"base": 6500, "max": 6502},
                    },
                },
                "cells": [
                    {
                        "record": "cell-eaws-lh1",
                        "node_ipv4": "172.19.255.11",
                        "host_ports": {
                            "aws_api": 4567,
                            "ecr_registry": 15101,
                            "eks_api_server": {"base": 6510, "max": 6512},
                        },
                    }
                ],
            }
        }

    @override
    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_config(self, config: dict[str, Any]) -> None:
        self.clusters_path.write_text(yaml.dump(config), encoding="utf-8")

    def test_valid_network_configuration(self) -> None:
        self.write_config(self.config)
        validate_cluster_network(self.clusters_path)

    def test_overlapping_host_ports_fail(self) -> None:
        cfg = dict(self.config)
        cell = dict(cfg["local"]["cells"][0])
        ports = dict(cell["host_ports"])
        ports["aws_api"] = OVERLAPPING_AWS_API_PORT  # Overlaps control aws_api
        cell["host_ports"] = ports
        cfg["local"]["cells"] = [cell]
        self.write_config(cfg)
        with _assert_raises(ClusterNetworkError):
            validate_cluster_network(self.clusters_path)

    def test_ip_in_dynamic_range_fails(self) -> None:
        cfg = dict(self.config)
        ctrl = dict(cfg["local"]["control"])
        ctrl["node_ipv4"] = "172.19.0.50"  # In dynamic range 172.19.0.0/24
        cfg["local"]["control"] = ctrl
        self.write_config(cfg)
        with _assert_raises(ClusterNetworkError):
            validate_cluster_network(self.clusters_path)


if __name__ == "__main__":
    unittest.main()
