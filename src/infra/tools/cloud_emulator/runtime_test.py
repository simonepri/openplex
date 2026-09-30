"""Protects retained fleet identities and data across runtime lifecycle commands."""

from __future__ import annotations

import base64
import copy
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

import yaml

from infra.tools.cloud_emulator import runtime


class RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / ".git").mkdir()
        (self.root / "src/infra/tools/cloud_emulator").mkdir(parents=True)
        (self.root / "src/infra/terraform/deployments/local").mkdir(parents=True)
        (self.root / "src/infra/terraform/deployments/local").mkdir(parents=True, exist_ok=True)
        (self.root / "src/infra/terraform/deployments/local/deployment.yaml").write_text(
            "clusters:\n"
            "  ctrl-eaws-lh1:\n"
            "    role: ctrl\n"
            "  cell-eaws-lh1:\n"
            "    provider: floci\n"
            "    role: cell\n"
            "    workspaces:\n"
            "      enabled: true\n"
            "      resource_envelope:\n"
            "        storage_gib: {default: 32, max: 48, min: 32}\n"
            "network:\n"
            "  services:\n"
            "    floci: 172.19.0.2\n"
            "    git: 172.19.255.21\n"
            "    origin_registry: 172.19.255.22\n",
            encoding="utf-8",
        )

    def test_docker_capacity_preflight_runs_before_reusing_a_running_daemon(self) -> None:
        with (
            patch.object(runtime, "check_docker_running", return_value=True),
            patch.object(runtime, "trim_colima_sparse_disk") as trim,
            patch.object(
                runtime,
                "validate_local_workspace_storage_headroom",
                return_value=100,
            ) as workspace,
            patch.object(runtime, "validate_local_docker_storage_headroom") as docker,
        ):
            runtime.ensure_docker_local(self.root, require_capacity=True)

        trim.assert_called_once_with()
        workspace.assert_called_once_with(self.root)
        docker.assert_called_once_with(100)

    def test_colima_autostart_preserves_the_fleet_memory_budget(self) -> None:
        with (
            patch.object(runtime, "check_docker_running", side_effect=[False, True]),
            patch.object(runtime.shutil, "which", return_value="/usr/local/bin/colima"),
            patch.object(runtime.subprocess, "run", return_value=Mock(returncode=0)) as run,
        ):
            runtime.ensure_docker_local(self.root)

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--memory") + 1], "40")
        self.assertEqual(arguments[arguments.index("--cpu") + 1], "14")
        self.assertEqual(arguments[arguments.index("--disk") + 1], "256")

    def test_capacity_preflight_rejects_workspace_reserve_below_eviction_threshold(self) -> None:
        values = self.root / "src/infra/argocd/components/rawfile_localpv/helm/values.yaml"
        values.parent.mkdir(parents=True)
        values.write_text("reservedCapacity: 51GiB\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "at least 52 GiB"):
            runtime.validate_local_workspace_storage_headroom(self.root)

        values.write_text("reservedCapacity: 52GiB\n", encoding="utf-8")
        self.assertEqual(runtime.validate_local_workspace_storage_headroom(self.root), 84)

    def test_capacity_preflight_rejects_low_colima_space(self) -> None:
        reports = [
            Mock(returncode=0, stdout="colima\n", stderr=""),
            Mock(
                returncode=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/vdb1 148038700 120000000 31457280 80% /var/lib/docker\n"
                ),
                stderr="",
            ),
        ]
        with patch.object(runtime.subprocess, "run", side_effect=reports):
            with self.assertRaisesRegex(RuntimeError, "84.0 GiB free in Colima"):
                runtime.validate_local_docker_storage_headroom(84)

    def test_compose_declares_the_git_daemon_security_and_lifecycle_contract(self) -> None:
        directory = Path(runtime.__file__).resolve().parent
        document = yaml.safe_load((directory / "compose.yaml").read_text(encoding="utf-8"))
        git = document["services"]["git"]

        self.assertNotIn("build", git)
        self.assertEqual(git["container_name"], "openplex-local-git")
        self.assertEqual(git["image"], "local/git-daemon:2.49.1")
        self.assertEqual(git["user"], runtime.GIT_DAEMON_USER)
        self.assertEqual(git["command"], list(runtime.GIT_DAEMON_COMMAND))
        self.assertEqual(git["networks"]["default"]["ipv4_address"], "172.19.255.21")
        self.assertEqual(git["ports"], ["127.0.0.1:9418:9418"])
        self.assertEqual(git["restart"], "unless-stopped")
        self.assertTrue(git["read_only"])
        self.assertEqual(git["cap_drop"], ["ALL"])
        self.assertEqual(git["security_opt"], ["no-new-privileges:true"])
        self.assertIn(
            "FROM docker.io/library/alpine:3.22.2@sha256:"
            "4b7ce07002c69e8f3d704a9c5d6fd3053be500b7f1c69fc0d80990c2ad8dd412",
            runtime.GIT_DAEMON_DOCKERFILE,
        )
        self.assertIn(
            "RUN apk add --no-cache git=2.49.1-r0 git-daemon=2.49.1-r0",
            runtime.GIT_DAEMON_DOCKERFILE,
        )
        self.assertIn("USER 65534:65534", runtime.GIT_DAEMON_DOCKERFILE)
        self.assertEqual(
            git["healthcheck"]["test"],
            [
                "CMD-SHELL",
                "git ls-remote git://127.0.0.1:9418/openplex.git HEAD >/dev/null",
            ],
        )
        self.assertEqual(
            git["volumes"],
            [
                {
                    "bind": {"create_host_path": False},
                    "read_only": True,
                    "source": "../../../../../.git",
                    "target": "/srv/git/openplex.git",
                    "type": "bind",
                }
            ],
        )

    def test_git_watcher_delivers_initial_head_and_retries_before_acknowledging(self) -> None:
        document = yaml.safe_load(Path(runtime.__file__).with_name("compose.yaml").read_text())
        command = document["services"]["git-watcher"]["command"][-1].replace("$$", "$")
        harness = r"""
        ip() { return 0; }
        curl() {
          attempt=$(cat "$WATCHER_TEST_ROOT/attempt")
          attempt=$((attempt + 1))
          printf '%s' "$attempt" > "$WATCHER_TEST_ROOT/attempt"
          printf '%s\n' "$@" > "$WATCHER_TEST_ROOT/args-$attempt"
          if [ "$attempt" -eq 1 ]; then printf 000; return 7; fi
          if [ "$attempt" -eq 2 ]; then printf 503; return 22; fi
          printf 200
        }
        ticks=0
        sleep() { ticks=$((ticks + 1)); if [ "$ticks" -ge 4 ]; then exit 0; fi; }
        """
        revision = "a" * 40
        for storage in ("loose", "packed", "detached"):
            with self.subTest(storage=storage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = root / "repo"
                repository.mkdir()
                ref = 'refs/heads/feature/quoted"branch'
                (repository / "HEAD").write_text(
                    f"{revision}\n" if storage == "detached" else f"ref: {ref}\n"
                )
                if storage == "loose":
                    path = repository / ref
                    path.parent.mkdir(parents=True)
                    path.write_text(f"{revision}\n")
                elif storage == "packed":
                    (repository / "packed-refs").write_text(f"# pack-refs\n{revision} {ref}\n")
                (root / "attempt").write_text("0")
                result = subprocess.run(
                    [
                        "/bin/sh",
                        "-c",
                        harness + command.replace("/srv/git/.git", '"$WATCHER_TEST_ROOT/repo"'),
                    ],
                    env={**os.environ, "WATCHER_TEST_ROOT": directory},
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                assert result.returncode == 0, result.stderr
                assert (root / "attempt").read_text() == "3"
                for attempt in range(1, 4):
                    arguments = (root / f"args-{attempt}").read_text().splitlines()
                    assert "--fail" in arguments
                    assert arguments[arguments.index("--connect-timeout") + 1] == "3"
                    assert arguments[arguments.index("--max-time") + 1] == "10"
                    payload = json.loads(arguments[arguments.index("-d") + 1])
                    assert payload["after"] == revision
                    assert payload["ref"] == ("HEAD" if storage == "detached" else ref)
                    assert payload["repository"] == {
                        "html_url": "http://172.19.255.21:9419/cgi-bin/git/openplex.git",
                        "default_branch": (
                            "HEAD" if storage == "detached" else 'feature/quoted"branch'
                        ),
                    }

    def test_git_http_endpoint_preserves_read_only_repository_access(self) -> None:
        document = yaml.safe_load(Path(runtime.__file__).with_name("compose.yaml").read_text())
        service = document["services"]["git-http"]
        assert service["image"] == document["services"]["git"]["image"]
        assert service["network_mode"] == "service:git"
        assert service["depends_on"] == {"git": {"condition": "service_healthy", "restart": True}}
        assert "ports" not in service
        assert service["user"] == runtime.GIT_DAEMON_USER
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["volumes"] == document["services"]["git"]["volumes"]
        assert "git ls-remote http://127.0.0.1:9419/" in service["healthcheck"]["test"][-1]
        assert document["services"]["git-watcher"]["depends_on"] == {
            "git-http": {"condition": "service_healthy"}
        }
        assert "-c http.receivepack=false http-backend" in runtime.GIT_DAEMON_DOCKERFILE

    def test_compose_declares_the_headscale_service(self) -> None:
        directory = Path(runtime.__file__).resolve().parent
        document = yaml.safe_load((directory / "compose.yaml").read_text(encoding="utf-8"))
        headscale = document["services"]["headscale"]

        self.assertEqual(headscale["container_name"], "openplex-local-headscale")
        self.assertEqual(headscale["networks"]["default"]["ipv4_address"], "172.19.255.23")
        self.assertEqual(headscale["ports"], ["127.0.0.1:8443:443"])
        self.assertEqual(headscale["restart"], "unless-stopped")
        self.assertEqual(
            document["volumes"]["headscale_state"]["name"], "openplex-local-headscale-data"
        )
        self.assertEqual(
            headscale["healthcheck"]["test"],
            ["CMD", "/ko-app/headscale", "health"],
        )

    def test_ensure_headscale_tls_generates_certificate_with_sans(self) -> None:
        runtime.ensure_headscale_tls(self.root)
        tls_dir = self.root / ".tmp/state/headscale/tls"
        self.assertTrue((tls_dir / "tls.crt").is_file())
        self.assertTrue((tls_dir / "tls.key").is_file())
        cnf = (tls_dir / "openssl.cnf").read_text(encoding="utf-8")
        self.assertIn("DNS.1 = headscale.ctrl-eaws-lh1.c.corp.local.internal\n", cnf)
        self.assertIn("DNS.2 = headscale.c.corp.local.internal\n", cnf)
        self.assertIn("DNS.3 = headscale.corp.local.internal\n", cnf)
        self.assertIn("DNS.4 = localhost\n", cnf)
        self.assertIn("IP.1 = 172.19.255.23\n", cnf)
        self.assertIn("IP.2 = 127.0.0.1\n", cnf)

    def test_ensure_floci_tls_generates_certificate_with_sans(self) -> None:
        runtime.ensure_floci_tls(self.root)
        tls_dir = self.root / ".tmp/state/floci/tls"
        self.assertTrue((tls_dir / "tls.crt").is_file())
        self.assertTrue((tls_dir / "tls.key").is_file())
        cnf = (tls_dir / "openssl.cnf").read_text(encoding="utf-8")
        self.assertIn("CN = localhost.floci.io\n", cnf)
        self.assertIn("DNS.1 = localhost\n", cnf)
        self.assertIn("DNS.2 = floci\n", cnf)
        self.assertIn("DNS.3 = origin-registry\n", cnf)
        self.assertIn("DNS.4 = *.localhost.floci.io\n", cnf)
        self.assertIn("DNS.5 = *.dkr.ecr.us-east-1.localhost.floci.io\n", cnf)
        self.assertIn("DNS.6 = *.dkr.ecr.us-west-2.localhost.floci.io\n", cnf)
        self.assertIn("DNS.7 = *.dkr.ecr.local.localhost.floci.io\n", cnf)
        self.assertIn("DNS.8 = *.corp.local.internal\n", cnf)
        self.assertIn("DNS.9 = s3.amazonaws.com\n", cnf)
        self.assertIn("DNS.10 = *.s3.amazonaws.com\n", cnf)
        self.assertIn("DNS.11 = *.s3.us-west-2.amazonaws.com\n", cnf)
        self.assertIn("IP.1 = 127.0.0.1\n", cnf)
        self.assertIn("IP.2 = 172.19.0.2\n", cnf)
        self.assertIn("IP.3 = 172.19.255.22\n", cnf)

    def test_compose_floci_address_must_match_the_certificate_inventory(self) -> None:
        docker = Docker(self.root)
        services = cast("dict[str, Any]", docker.document["services"])
        services["floci"]["networks"]["default"]["ipv4_address"] = "192.0.2.99"
        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "Floci address differs"):
                runtime.configuration(self.root)

    def test_floci_bootstrap_certificate_uses_configured_addresses(self) -> None:
        inventory_path = self.root / "src/infra/terraform/deployments/local/deployment.yaml"
        inventory = yaml.safe_load(inventory_path.read_text())
        inventory["network"]["services"].update(floci="192.0.2.99", origin_registry="192.0.2.98")
        inventory_path.write_text(yaml.safe_dump(inventory))
        runtime.ensure_floci_tls(self.root)
        config = (self.root / ".tmp/state/floci/tls/openssl.cnf").read_text()
        self.assertIn("IP.2 = 192.0.2.99\n", config)
        self.assertIn("IP.3 = 192.0.2.98\n", config)

    def test_local_deployment_matches_floci_kubernetes_version(self) -> None:
        directory = Path(runtime.__file__).resolve().parent
        document = yaml.safe_load((directory / "compose.yaml").read_text(encoding="utf-8"))
        environment = document["services"]["floci"]["environment"]
        image = environment["FLOCI_SERVICES_EKS_DEFAULT_IMAGE"]
        self.assertEqual(environment["FLOCI_SERVICES_EKS_IMAGE_TEMPLATE"], image)
        match = re.fullmatch(
            r"cluster/k3s-runtime:v(([0-9]+\.[0-9]+)(?:\.[0-9]+)?)-runtime[0-9]+", image
        )
        self.assertIsNotNone(match)
        assert match is not None

        deployment = (directory.parent.parent / "terraform/deployments/local/main.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'floci_kubernetes_version = "{match.group(2)}"', deployment)
        self.assertEqual(
            deployment.count("kubernetes_version            = local.floci_kubernetes_version"),
            2,
        )

    def test_local_deployment_keeps_floci_cluster_endpoint_private(self) -> None:
        directory = Path(runtime.__file__).resolve().parent
        deployment = (directory.parent.parent / "terraform/deployments/local/main.tf").read_text(
            encoding="utf-8"
        )

        self.assertEqual(len(re.findall(r"public_access_cidrs\s*= \[\]", deployment)), 2)

    def test_configuration_rejects_a_compose_git_build(self) -> None:
        docker = Docker(self.root)
        services = cast("dict[str, dict[str, object]]", docker.document["services"])
        services["git"]["build"] = {"context": "."}

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "must not build"):
                runtime.configuration(self.root)
        self.assertEqual(docker.mutations, [])

    def test_configuration_rejects_a_foreign_or_missing_git_directory(self) -> None:
        docker = Docker(self.root)
        services = cast("dict[str, dict[str, object]]", docker.document["services"])
        volumes = cast("list[dict[str, object]]", services["git"]["volumes"])
        source = volumes[0]
        source["source"] = "/foreign/.git"

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "this checkout"):
                runtime.configuration(self.root)

        source["source"] = str(self.root / ".git")
        (self.root / ".git").rmdir()
        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "this checkout"):
                runtime.configuration(self.root)
        self.assertEqual(docker.mutations, [])

    def test_configuration_rejects_git_address_outside_fleet_inventory(self) -> None:
        docker = Docker(self.root)
        (self.root / "src/infra/terraform/deployments/local").mkdir(parents=True, exist_ok=True)
        (self.root / "src/infra/terraform/deployments/local/deployment.yaml").write_text(
            "clusters:\n"
            "  ctrl-eaws-lh1:\n"
            "    role: ctrl\n"
            "network:\n"
            "  services:\n"
            "    floci: 172.19.0.2\n"
            "    git: 172.19.255.99\n"
            "    origin_registry: 172.19.255.22\n",
            encoding="utf-8",
        )

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "fleet inventory"):
                runtime.configuration(self.root)
        self.assertEqual(docker.mutations, [])

    def test_managed_git_validation_rejects_runtime_contract_drift(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            fleet = runtime.configuration(self.root)

        for case in (
            "address",
            "autoremove",
            "cap_add",
            "cap_drop",
            "entrypoint",
            "health",
            "mount",
            "privileged",
            "restart",
            "security_opt",
        ):
            with self.subTest(case=case):
                git = copy.deepcopy(docker.containers["fixture-git"])
                if case == "address":
                    git["NetworkSettings"]["Networks"]["fixture-network"]["IPAMConfig"] = {
                        "IPv4Address": "172.19.255.99"
                    }
                    git["NetworkSettings"]["Networks"]["fixture-network"]["IPAddress"] = (
                        "172.19.255.99"
                    )
                elif case == "autoremove":
                    git["HostConfig"]["AutoRemove"] = True
                elif case == "cap_add":
                    git["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
                elif case == "cap_drop":
                    git["HostConfig"]["CapDrop"] = []
                elif case == "entrypoint":
                    git["Config"]["Entrypoint"] = ["sh"]
                elif case == "health":
                    git["State"]["Health"]["Status"] = "unhealthy"
                elif case == "mount":
                    git["Mounts"][0]["Source"] = "/foreign/.git"
                elif case == "privileged":
                    git["HostConfig"]["Privileged"] = True
                elif case == "restart":
                    git["HostConfig"]["RestartPolicy"]["Name"] = "no"
                else:
                    git["HostConfig"]["SecurityOpt"] = []

                with self.assertRaises(RuntimeError):
                    runtime.validate_managed_git_daemon(git, fleet)

    def test_registry_repair_retains_container_and_other_networks_and_is_idempotent(self) -> None:
        for running in (True, False):
            with self.subTest(running=running):
                docker = Docker(self.root)
                docker.add_fleet(managed=True)
                registry = docker.containers["floci-fixture-ecr-registry"]
                registry["State"]["Running"] = running
                registry["NetworkSettings"]["Networks"]["fixture-network"] = {
                    "IPAddress": "172.19.0.3",
                    "IPAMConfig": None,
                    "Aliases": ["existing-alias"],
                }
                before = copy.deepcopy(docker.containers)
                volumes = copy.deepcopy(docker.volumes)

                with patch.object(runtime.subprocess, "run", side_effect=docker.run):
                    runtime.reconcile_registry(self.root)
                    mutations = list(docker.mutations)
                    runtime.reconcile_registry(self.root)

                endpoint = registry["NetworkSettings"]["Networks"]["fixture-network"]
                self.assertEqual(endpoint["IPAddress"], "172.19.255.22" if running else "")
                self.assertEqual(endpoint["IPAMConfig"]["IPv4Address"], "172.19.255.22")
                self.assertEqual(set(endpoint["Aliases"]), {"existing-alias", "origin-registry"})
                self.assertEqual(docker.mutations, mutations)
                self.assertEqual(docker.volumes, volumes)
                before["floci-fixture-ecr-registry"]["NetworkSettings"]["Networks"][
                    "fixture-network"
                ] = endpoint
                self.assertEqual(docker.containers, before)

    def test_registry_rejects_occupied_address_before_disconnect(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        registry = docker.containers["floci-fixture-ecr-registry"]
        registry["NetworkSettings"]["Networks"]["fixture-network"]["IPAddress"] = "172.19.0.3"
        docker.containers["unrelated"]["NetworkSettings"]["Networks"]["fixture-network"] = {
            "IPAddress": "172.19.255.22",
        }
        before = copy.deepcopy(docker.containers)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                runtime.reconcile_registry(self.root)

        self.assertEqual(docker.containers, before)
        self.assertEqual(docker.mutations, [])

    def test_provider_prefixed_registry_is_attached_without_changing_its_identity(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        registry = docker.containers.pop("floci-fixture-ecr-registry")
        registry["Name"] = "/floci-aws-fixture-ecr-registry"
        docker.containers["floci-aws-fixture-ecr-registry"] = registry
        del registry["NetworkSettings"]["Networks"]["fixture-network"]
        before = copy.deepcopy(docker.containers)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.reconcile_registry(self.root)
            mutations = list(docker.mutations)
            runtime.reconcile_registry(self.root)

        endpoint = registry["NetworkSettings"]["Networks"]["fixture-network"]
        self.assertEqual(endpoint["IPAddress"], "172.19.255.22")
        self.assertEqual(endpoint["IPAMConfig"]["IPv4Address"], "172.19.255.22")
        self.assertEqual(endpoint["Aliases"], ["origin-registry"])
        self.assertEqual(docker.mutations, mutations)
        before["floci-aws-fixture-ecr-registry"]["NetworkSettings"]["Networks"][
            "fixture-network"
        ] = endpoint
        self.assertEqual(docker.containers, before)

    def test_duplicate_registry_names_are_rejected_before_mutation(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        duplicate = copy.deepcopy(docker.containers["floci-fixture-ecr-registry"])
        duplicate["Id"] = "second-registry-id"
        duplicate["Name"] = "/floci-aws-fixture-ecr-registry"
        docker.containers["floci-aws-fixture-ecr-registry"] = duplicate
        before = copy.deepcopy(docker.containers)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "Multiple Floci registries"):
                runtime.reconcile_registry(self.root)

        self.assertEqual(docker.containers, before)
        self.assertEqual(docker.mutations, [])

    def test_missing_runtime_image_build_uses_the_configured_bazel_root(self) -> None:
        manifest = self.root / "src/third_party/k3s-io/k3s/images.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('tag = "fixture"\n', encoding="utf-8")
        for configured_root in ("", str(self.root / "custom-bazel")):
            with (
                self.subTest(configured_root=configured_root),
                patch.dict(runtime.os.environ, {"BAZEL_OUTPUT_ROOT": configured_root}),
                patch.object(runtime.subprocess, "run", return_value=Mock(returncode=1)),
                patch.object(runtime, "run") as build,
            ):
                runtime._ensure_k3s_runtime_image(self.root, timeout=45)

                expected_root = configured_root or str(self.root / ".tmp/state/bazel")
                build.assert_called_once_with(
                    [
                        "bazel",
                        f"--output_user_root={expected_root}",
                        "run",
                        "//src/third_party/k3s-io/k3s:load",
                    ],
                    cwd=self.root,
                    timeout=45,
                )

    def test_stopped_registry_keeps_its_correct_persistent_endpoint(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        registry = docker.containers["floci-fixture-ecr-registry"]
        registry["State"]["Running"] = False
        registry["NetworkSettings"]["Networks"]["fixture-network"]["IPAddress"] = ""
        before = copy.deepcopy(docker.containers)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.reconcile_registry(self.root)

        self.assertEqual(docker.containers, before)
        self.assertEqual(docker.mutations, [])

    def test_failed_registry_connect_restores_previous_attachment(self) -> None:
        for failure in ("error", "timeout"):
            with self.subTest(failure=failure):
                docker = Docker(self.root)
                docker.add_fleet(managed=True)
                registry = docker.containers["floci-fixture-ecr-registry"]
                registry["NetworkSettings"]["Networks"]["fixture-network"] = {
                    "IPAddress": "172.19.0.3",
                    "IPAMConfig": None,
                    "Aliases": ["original"],
                }
                docker.fail_registry_connect = failure == "error"
                docker.timeout_registry_connect = failure == "timeout"
                before = copy.deepcopy(docker.containers)

                with patch.object(runtime.subprocess, "run", side_effect=docker.run):
                    with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                        runtime.reconcile_registry(self.root)

                before["floci-fixture-ecr-registry"]["NetworkSettings"]["Networks"][
                    "fixture-network"
                ]["IPAMConfig"] = {"IPv4Address": "172.19.0.3"}
                self.assertEqual(docker.containers, before)
                self.assertEqual(registry["State"], {"Running": True})

    def test_registry_with_foreign_labels_is_rejected_before_mutation(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        docker.containers["floci-fixture-ecr-registry"]["Config"]["Labels"]["floci_namespace"] = (
            "other"
        )

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaises(RuntimeError):
                runtime.reconcile_registry(self.root)

        self.assertEqual(docker.mutations, [])

    def test_post_apply_requires_registry_from_this_fleet(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        del docker.containers["floci-fixture-ecr-registry"]

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.reconcile_registry(self.root, required=False)
            with self.assertRaises(RuntimeError):
                runtime.reconcile_registry(self.root)

        self.assertEqual(docker.mutations, [])

    def test_missing_metadata_rejects_start_before_changing_retained_services(self) -> None:
        docker = Docker(self.root)
        docker.add_services()
        before = copy.deepcopy(docker.containers)
        volumes = copy.deepcopy(docker.volumes)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaises(RuntimeError):
                runtime.start(self.root)

        self.assertEqual(docker.containers, before)
        self.assertEqual(docker.mutations, [])
        self.assertEqual(docker.volumes, volumes)

    def test_unmanaged_parent_handover_retains_service_ids_and_volume_contents(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=False)
        children = copy.deepcopy(docker.containers)
        del children["fixture-floci"]
        volumes = copy.deepcopy(docker.volumes)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.start(self.root)

        self.assertNotEqual(docker.containers["fixture-floci"]["Id"], "old-parent")
        self.assertEqual(
            docker.containers["fixture-floci"]["Mounts"][0]["Name"], "fixture-metadata"
        )
        self.assertEqual(
            docker.containers["fixture-floci"]["NetworkSettings"]["Networks"],
            {"fixture-network": {}},
        )
        self.assertEqual(docker.volumes, volumes)
        for name, previous in children.items():
            with self.subTest(container=name):
                self.assertEqual(docker.containers[name]["Id"], previous["Id"])
                self.assertEqual(docker.containers[name]["Mounts"], previous["Mounts"])
                self.assertTrue(docker.containers[name]["State"]["Running"])
                if name.startswith("floci-fixture-"):
                    self.assertEqual(
                        docker.containers[name]["HostConfig"]["RestartPolicy"]["Name"],
                        "unless-stopped",
                    )
        self.assertEqual(
            [args for args in docker.mutations if args[1] == "rm"], [["docker", "rm", "old-parent"]]
        )
        pull = next(index for index, args in enumerate(docker.commands) if "pull" in args)
        first_mutation = min(docker.commands.index(args) for args in docker.mutations)
        self.assertLess(pull, first_mutation)

    def test_retained_node_images_must_match_pinned_tag(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        images_toml = self.root / "src/third_party/k3s-io/k3s/images.toml"
        images_toml.parent.mkdir(parents=True, exist_ok=True)
        images_toml.write_text('tag = "v1.36.1-runtime7"\n', encoding="utf-8")

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            fleet = runtime.configuration(self.root)

        node_container = {
            "Name": f"/floci-{fleet.namespace}-eks-ctrl-eaws-lh1",
            "Id": "old-ctrl-id",
            "Image": "sha256:stale-id",
            "Config": {"Image": "cluster/k3s-runtime:v1.36.1-runtime5"},
        }

        recreated = {
            "Id": "new-ctrl-id",
            "Name": node_container["Name"],
            "Image": "sha256:pinned-id",
        }
        with (
            patch.object(
                runtime,
                "_resolve_image_tag_and_id",
                side_effect=lambda ref: (
                    ("v1.36.1-runtime7", "sha256:pinned-id")
                    if "runtime7" in ref
                    else ("v1.36.1-runtime5", "sha256:stale-id")
                ),
            ),
            patch.object(
                runtime,
                "_recreate_node_container",
                return_value=recreated,
            ) as mock_recreate,
        ):
            containers = [dict(node_container)]
            runtime._validate_retained_node_images(fleet, containers, self.root)
            mock_recreate.assert_called_once()
            self.assertEqual(containers[0]["Id"], "new-ctrl-id")

        with patch.object(
            runtime,
            "_resolve_image_tag_and_id",
            side_effect=lambda ref: (
                ("unknown", "") if "runtime7" in ref else ("v1.36.1-runtime5", "sha256:stale-id")
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"Container floci-fixture-eks-ctrl-eaws-lh1 is running image v1.36.1-runtime5, expected v1.36.1-runtime7 \(unknown\)",
            ):
                runtime._validate_retained_node_images(fleet, [node_container], self.root)

    def test_pull_failure_leaves_unmanaged_parent_and_children_untouched(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=False)
        docker.fail_pull = True
        containers = copy.deepcopy(docker.containers)
        volumes = copy.deepcopy(docker.volumes)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaises(RuntimeError):
                runtime.start(self.root)

        self.assertEqual(docker.containers, containers)
        self.assertEqual(docker.volumes, volumes)
        self.assertEqual(docker.mutations, [])

    def test_legacy_git_handover_builds_candidate_before_retirement(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        docker.add_git("legacy-git", managed=False)
        floci = {
            name: copy.deepcopy(container)
            for name, container in docker.containers.items()
            if name == "fixture-floci" or name.startswith("floci-fixture-")
        }

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.start(self.root)

        git = docker.containers["fixture-git"]
        self.assertEqual(git["Id"], "compose-git")
        self.assertEqual(
            git["Config"]["Labels"],
            {
                "com.docker.compose.project": "fixture-compose",
                "com.docker.compose.service": "git",
            },
        )
        self.assertEqual(git["Mounts"][0]["Source"], str(self.root / ".git"))
        self.assertFalse(git["Mounts"][0]["RW"])
        build = next(
            index
            for index, arguments in enumerate(docker.commands)
            if arguments[:2] == ["docker", "build"]
        )
        self.assertEqual(
            docker.commands[build],
            [
                "docker",
                "build",
                "--tag",
                "local/git-daemon:2.49.1",
                "-",
            ],
        )
        self.assertEqual(docker.stdins[build], runtime.GIT_DAEMON_DOCKERFILE)
        retirement = next(
            index
            for index, arguments in enumerate(docker.commands)
            if arguments[:2] == ["docker", "stop"] and arguments[-1] == "legacy-git"
        )
        self.assertLess(build, retirement)
        self.assertIn(["docker", "rm", "legacy-git"], docker.mutations)
        for name, previous in floci.items():
            self.assertEqual(docker.containers[name]["Id"], previous["Id"])
            self.assertEqual(docker.containers[name]["Mounts"], previous["Mounts"])

    def test_git_build_failure_leaves_legacy_daemon_untouched(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        docker.add_git("legacy-git", managed=False)
        docker.fail_build = True
        containers = copy.deepcopy(docker.containers)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            with self.assertRaisesRegex(RuntimeError, "classic builder failed"):
                runtime.start(self.root)

        self.assertEqual(docker.containers, containers)
        self.assertEqual(docker.mutations, [])

    def test_mitigated_legacy_git_restart_policy_is_adopted(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        docker.add_git("legacy-git", managed=False)
        docker.containers["fixture-git"]["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.start(self.root)

        self.assertEqual(docker.containers["fixture-git"]["Id"], "compose-git")
        self.assertEqual(
            docker.containers["fixture-git"]["HostConfig"]["RestartPolicy"]["Name"],
            "unless-stopped",
        )

    def test_git_handover_rejects_foreign_endpoint_name_and_mount_without_mutation(self) -> None:
        for case in ("name", "address", "port", "mount"):
            with self.subTest(case=case):
                docker = Docker(self.root)
                docker.add_fleet(managed=True)
                if case == "name":
                    docker.containers["fixture-git"]["Config"]["Image"] = "foreign/image"
                    docker.containers["fixture-git"]["Config"]["Labels"] = {}
                elif case == "address":
                    del docker.containers["fixture-git"]
                    docker.containers["unrelated"]["NetworkSettings"]["Networks"][
                        "fixture-network"
                    ] = {
                        "IPAddress": "172.19.255.21",
                        "IPAMConfig": {"IPv4Address": "172.19.255.21"},
                    }
                elif case == "port":
                    docker.containers["unrelated"]["HostConfig"]["PortBindings"] = {
                        "9418/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9418"}]
                    }
                else:
                    docker.containers["fixture-git"]["Mounts"][0]["Source"] = "/foreign/.git"
                    docker.containers["fixture-git"]["Config"]["Labels"] = {}
                containers = copy.deepcopy(docker.containers)

                with patch.object(runtime.subprocess, "run", side_effect=docker.run):
                    with self.assertRaises(RuntimeError):
                        runtime.start(self.root)

                self.assertEqual(docker.containers, containers)
                self.assertEqual(docker.mutations, [])

    def test_stop_retains_containers_and_volumes_and_leaves_other_fleets_running(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        containers = copy.deepcopy(docker.containers)
        volumes = copy.deepcopy(docker.volumes)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.stop(self.root)
            first_stop = list(docker.mutations)
            runtime.stop(self.root)

        self.assertEqual(set(docker.containers), set(containers))
        self.assertEqual(docker.volumes, volumes)
        self.assertEqual(docker.mutations, first_stop)
        self.assertTrue(docker.mutations)
        self.assertTrue(all(args[:2] == ["docker", "stop"] for args in docker.mutations))
        self.assertEqual(docker.mutations[0][-1], "old-parent")
        self.assertFalse(docker.containers["fixture-git"]["State"]["Running"])
        for name, previous in containers.items():
            with self.subTest(container=name):
                self.assertEqual(docker.containers[name]["Id"], previous["Id"])
                self.assertEqual(docker.containers[name]["Mounts"], previous["Mounts"])
                self.assertEqual(
                    docker.containers[name]["State"]["Running"],
                    name in {"unrelated", "floci-fixture2-eks-cell", "floci-fixture2-ecr-registry"},
                )

    def test_git_helpers_start_with_the_runtime_and_stop_before_the_shared_namespace(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        services = cast("dict[str, Any]", docker.document["services"])
        for service in ("git-http", "git-watcher"):
            name = f"fixture-{service}"
            services[service] = {"container_name": name}
            docker.containers[name] = {
                "Id": name,
                "Name": f"/{name}",
                "State": {"Running": True},
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "fixture-compose",
                        "com.docker.compose.service": service,
                    }
                },
            }
        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            fleet = runtime.configuration(self.root)
            with patch.object(runtime, "compose") as compose:
                runtime._start_services(fleet, timeout=30)
            runtime.stop(self.root)
        assert "git-http" in compose.call_args.args
        assert "git-watcher" in compose.call_args.args
        stopped = [arguments[-1] for arguments in docker.mutations if arguments[1] == "stop"]
        assert stopped.index("fixture-git-watcher") < stopped.index("fixture-git-http")
        assert stopped.index("fixture-git-http") < stopped.index("git-id")

    def test_cluster_volumes_cover_attached_and_orphaned_data_of_this_fleet_only(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        docker.volumes["floci-fixture-eks-retired"] = b"orphaned cluster data"
        docker.volumes["floci-aws-fixture-eks-cell"] = b"aws-prefixed cluster data"

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            volumes = runtime.cluster_volumes(self.root)

        self.assertEqual(
            volumes, ["cluster-data", "floci-aws-fixture-eks-cell", "floci-fixture-eks-retired"]
        )
        self.assertFalse(docker.mutations)

    def test_remove_volumes_deletes_retained_cluster_data_and_skips_missing(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        del docker.containers["floci-fixture-eks-cell"]

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.remove_volumes(["cluster-data", "already-removed"])
            runtime.remove_volumes(["cluster-data"])

        self.assertNotIn("cluster-data", docker.volumes)
        self.assertIn("registry-data", docker.volumes)
        self.assertEqual(docker.mutations, [["docker", "volume", "rm", "cluster-data"]])

    def test_remove_volumes_rejects_volumes_still_in_use(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)

        with (
            patch.object(runtime.subprocess, "run", side_effect=docker.run),
            self.assertRaises(RuntimeError),
        ):
            runtime.remove_volumes(["cluster-data"])

        self.assertIn("cluster-data", docker.volumes)

    def test_stop_start_retains_managed_git_identity_and_mount(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        git = copy.deepcopy(docker.containers["fixture-git"])

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.stop(self.root)
            runtime.start(self.root)

        self.assertEqual(docker.containers["fixture-git"]["Id"], git["Id"])
        self.assertEqual(docker.containers["fixture-git"]["Mounts"], git["Mounts"])
        self.assertTrue(docker.containers["fixture-git"]["State"]["Running"])

    def test_empty_runtime_creates_storage_before_starting_the_emulator(self) -> None:
        docker = Docker(self.root)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.start(self.root)

        self.assertTrue(docker.containers["fixture-floci"]["State"]["Running"])
        git = docker.containers["fixture-git"]
        self.assertTrue(git["State"]["Running"])
        self.assertEqual(git["State"]["Health"]["Status"], "healthy")
        self.assertEqual(git["HostConfig"]["RestartPolicy"]["Name"], "unless-stopped")
        self.assertTrue(git["HostConfig"]["ReadonlyRootfs"])
        self.assertFalse(git["Mounts"][0]["RW"])
        self.assertEqual(docker.volumes, {"fixture-metadata": b""})
        self.assertEqual(docker.networks, {"fixture-network"})
        self.assertFalse(
            any(args[:2] in (["docker", "stop"], ["docker", "rm"]) for args in docker.mutations)
        )

    def test_dynamic_addresses_exclude_static_services_without_recreating_retained_network(
        self,
    ) -> None:
        docker = Docker(self.root)
        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.start(self.root)
            runtime.start(self.root)

        creations = [
            args for args in docker.commands if args[:3] == ["docker", "network", "create"]
        ]
        self.assertEqual(len(creations), 1)
        arguments = creations[0]
        pool = ipaddress.IPv4Network(arguments[arguments.index("--ip-range") + 1])
        subnet = ipaddress.IPv4Network(arguments[arguments.index("--subnet") + 1])
        self.assertTrue(pool.subnet_of(subnet))
        self.assertNotIn(ipaddress.ip_address(arguments[arguments.index("--gateway") + 1]), pool)
        document = yaml.safe_load(
            Path(runtime.__file__).with_name("compose.yaml").read_text(encoding="utf-8")
        )
        addresses = [
            endpoint["ipv4_address"]
            for service in document["services"].values()
            for endpoint in service.get("networks", {}).values()
            if endpoint and "ipv4_address" in endpoint
        ]
        self.assertIn("172.19.0.2", addresses)
        for address in addresses:
            with self.subTest(address=address):
                self.assertNotIn(ipaddress.ip_address(address), pool)
        self.assertFalse(any(args[:3] == ["docker", "network", "rm"] for args in docker.commands))

    def test_repeated_start_uses_compose_without_removing_managed_parent(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        containers = copy.deepcopy(docker.containers)
        volumes = copy.deepcopy(docker.volumes)

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.start(self.root)
            runtime.start(self.root)

        for name in docker.containers:
            if name != "fixture-floci" and name.startswith("floci-fixture-"):
                containers[name]["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
        self.assertEqual(docker.containers, containers)
        self.assertEqual(docker.volumes, volumes)
        self.assertFalse(
            any(args[:2] in (["docker", "stop"], ["docker", "rm"]) for args in docker.mutations)
        )
        self.assertEqual(
            sum(args[0] == "docker-compose" and "up" in args for args in docker.commands), 2
        )
        self.assertEqual(sum(args[:2] == ["docker", "update"] for args in docker.commands), 1)

    def test_start_repairs_child_restart_policy_without_replacing_identity(self) -> None:
        docker = Docker(self.root)
        docker.add_fleet(managed=True)
        children = {
            name: container["Id"]
            for name, container in docker.containers.items()
            if name.startswith("floci-fixture-")
        }

        with patch.object(runtime.subprocess, "run", side_effect=docker.run):
            runtime.start(self.root)

        self.assertEqual(
            [args for args in docker.mutations if args[:2] == ["docker", "update"]],
            [["docker", "update", "--restart", "unless-stopped", *children.values()]],
        )
        for name, identifier in children.items():
            self.assertEqual(docker.containers[name]["Id"], identifier)
            self.assertEqual(
                docker.containers[name]["HostConfig"]["RestartPolicy"]["Name"],
                "unless-stopped",
            )

    def test_unsafe_shutdown_settings_reject_handover_and_stop_before_mutation(self) -> None:
        settings = {
            "FLOCI_SERVICES_EKS_KEEP_RUNNING_ON_SHUTDOWN": "false",
            "FLOCI_SERVICES_ECR_KEEP_RUNNING_ON_SHUTDOWN": "false",
            "FLOCI_STORAGE_MODE": "memory",
            "FLOCI_STORAGE_PRUNE_VOLUMES_ON_DELETE": "true",
        }
        for operation in (runtime.start, runtime.stop):
            for name, value in settings.items():
                with self.subTest(operation=operation.__name__, setting=name):
                    docker = Docker(self.root)
                    docker.add_fleet(managed=False)
                    parent = docker.containers["fixture-floci"]
                    parent["Config"]["Env"] = [
                        f"{name}={value}" if item.startswith(f"{name}=") else item
                        for item in parent["Config"]["Env"]
                    ]
                    before = copy.deepcopy(docker.containers)

                    with patch.object(runtime.subprocess, "run", side_effect=docker.run):
                        with self.assertRaises(RuntimeError):
                            operation(self.root)

                    self.assertEqual(docker.containers, before)
                    self.assertEqual(docker.mutations, [])

    def test_reconcile_floci_bridge_does_not_issue_bridge_network_connect(self) -> None:
        fleet = cast("runtime.Fleet", Mock())
        commands: list[list[str]] = []

        def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(runtime, "run", side_effect=fake_run):
            runtime.reconcile_floci_bridge(fleet)

        self.assertEqual(commands, [])

    def test_reconcile_eks_containers_reconciles_token_webhook_without_ip_addr_add(self) -> None:
        commands: list[list[str]] = []

        def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(list(args))
            if args[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps([{"State": {"Running": True}}]), ""
                )
            if (
                args[:3] == ["docker", "exec", "floci-aws-test-eks-ctrl-eaws-lh1"]
                and "test -f /etc/token-webhook.yaml" in args[-1]
            ):
                return subprocess.CompletedProcess(args, 0, "patch\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        children = [
            {"Name": "/floci-aws-test-eks-ctrl-eaws-lh1"},
            {"Name": "/floci-aws-test-eks-cell-eaws-lh1"},
        ]

        with patch.object(runtime, "run", side_effect=fake_run):
            runtime.reconcile_eks_containers(children)

        # Confirm no ip addr commands or sysfs mac grep commands were run
        for cmd in commands:
            cmd_str = " ".join(cmd)
            self.assertNotIn("ip addr add", cmd_str)
            self.assertNotIn("/sys/class/net", cmd_str)

        # Confirm token-webhook sed patch was invoked for the container needing it
        sed_commands = [cmd for cmd in commands if "sed" in cmd]
        self.assertEqual(len(sed_commands), 1)
        self.assertTrue(any("172.19.0.2:4566" in arg for arg in sed_commands[0]))

    def test_reconcile_eks_containers_skips_stopped_containers(self) -> None:
        commands: list[list[str]] = []

        def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(list(args))
            if args[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps([{"State": {"Running": False}}]), ""
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        children = [{"Name": "/floci-aws-test-eks-ctrl-eaws-lh1"}]

        with patch.object(runtime, "run", side_effect=fake_run):
            runtime.reconcile_eks_containers(children)

        # Inspect was run, but no exec commands followed
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:3], ["docker", "inspect", "floci-aws-test-eks-ctrl-eaws-lh1"])


class RuntimeTlsTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls: list[list[str]] = []
        self.secrets: dict[tuple[str, str], dict[str, Any]] = {}
        self.inject_ca = True
        self.merge_ca = False
        self.webhooks: dict[str, dict[str, Any]] = {
            context: {
                "metadata": {"resourceVersion": "1"},
                "webhooks": [
                    {
                        "name": "pod-identity.eks.floci.io",
                        "clientConfig": {
                            "url": f"https://172.19.0.2:4566/_floci/eks/clusters/{context}/pod-identity-webhook/scope/000000000000",
                            "caBundle": base64.b64encode(b"Floci internal authority").decode(),
                        },
                    }
                ],
            }
            for context in ("ctrl", "cell")
        }
        local = self.root / "src/infra/tools/cloud_emulator/headscale"
        local.mkdir(parents=True)
        (local / "config.yaml").write_text(
            "server_url: https://headscale.ctrl-eaws-lh1.c.corp.local.internal\n",
            encoding="utf-8",
        )
        (self.root / "src/infra/terraform/deployments/local").mkdir(parents=True, exist_ok=True)
        (self.root / "src/infra/terraform/deployments/local/deployment.yaml").write_text(
            "clusters:\n  ctrl:\n    role: ctrl\n  cell:\n    role: cell\n"
            "network:\n  services: {floci: 172.19.0.2, origin_registry: 172.19.255.22}\n",
            encoding="utf-8",
        )
        self.command = runtime.run
        (self.root / "root.cnf").write_text(
            "[req]\ndistinguished_name=dn\n[dn]\n[ca]\n"
            "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n",
            encoding="utf-8",
        )
        self.command([
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "3",
            "-config",
            str(self.root / "root.cnf"),
            "-extensions",
            "ca",
            "-subj",
            "/CN=Test authority",
            "-keyout",
            str(self.root / "root.key"),
            "-out",
            str(self.root / "root.crt"),
        ])
        authority = base64.b64encode((self.root / "root.crt").read_bytes()).decode()
        for context in ("ctrl", "cell"):
            self.secrets[context, "cluster-local-ca"] = {"data": {"tls.crt": authority}}
        runtime.ensure_headscale_tls(self.root)
        runtime.ensure_floci_tls(self.root)
        for service in ("headscale", "floci"):
            source = self.root / ".tmp/state" / service / "tls"
            # Sign generated CSRs without replacing the temporary bootstrap leaves.
            config = (source / "openssl.cnf").read_text(encoding="utf-8")
            config = config.replace(
                "[v3_req]\n",
                "[v3_req]\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n",
            )
            if service == "floci":
                config += "DNS.12 = localhost.floci.io\n"
            (source / "signed.cnf").write_text(config, encoding="utf-8")
            self.command([
                "openssl",
                "x509",
                "-req",
                "-in",
                str(source / "tls.csr"),
                "-CA",
                str(self.root / "root.crt"),
                "-CAkey",
                str(self.root / "root.key"),
                "-CAcreateserial",
                "-days",
                "2",
                "-out",
                str(source / "signed.crt"),
                "-extfile",
                str(source / "signed.cnf"),
                "-extensions",
                "v3_req",
            ])
            self.secrets["ctrl", f"local-{service}-tls"] = {
                "data": {
                    "tls.crt": base64.b64encode((source / "signed.crt").read_bytes()).decode(),
                    "tls.key": base64.b64encode((source / "tls.key").read_bytes()).decode(),
                }
            }
        self.fleet = Mock()

    def boundary(
        self, arguments: list[str], *, stdin: str | None = None, timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        if arguments[0] != "kubectl":
            return self.command(arguments, stdin=stdin, timeout=timeout)
        self.calls.append(arguments)
        context = arguments[2]
        if "wait" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "Ready", "")
        if "mutatingwebhookconfiguration" in arguments:
            return self.webhook_boundary(arguments, stdin=stdin)
        if "apply" in arguments:
            secret = json.loads(stdin or "")
            self.secrets[context, secret["metadata"]["name"]] = secret
            return subprocess.CompletedProcess(arguments, 0, "configured", "")
        name = arguments[arguments.index("secret") + 1]
        secret = self.secrets.get((context, name))
        if "patch" in arguments:
            assert secret is not None
            secret.update(json.loads(stdin or ""))
            return subprocess.CompletedProcess(arguments, 0, "patched", "")
        if "jsonpath={.data.tls\\.crt}" in arguments:
            output = secret["data"]["tls.crt"] if secret else ""
        else:
            output = json.dumps(secret) if secret else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    def webhook_boundary(
        self, arguments: list[str], *, stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        context = arguments[2]
        webhook = self.webhooks[context]
        if "patch" in arguments:
            patch_document = json.loads(stdin or "")
            if "--type=json" in arguments:
                if patch_document[0]["value"] != webhook["metadata"]["resourceVersion"]:
                    raise RuntimeError("resource version conflict")
                webhook["webhooks"][0]["clientConfig"]["caBundle"] = patch_document[1]["value"]
            else:
                webhook["metadata"].update(patch_document["metadata"])
        elif self.inject_ca and webhook["metadata"].get("annotations"):
            authority = self.secrets["ctrl", "cluster-local-ca"]["data"]["tls.crt"]
            client = webhook["webhooks"][0]["clientConfig"]
            if self.merge_ca:
                bundle = base64.b64decode(client["caBundle"])
                if base64.b64decode(authority) not in bundle:
                    client["caBundle"] = base64.b64encode(
                        bundle + base64.b64decode(authority)
                    ).decode()
            else:
                client["caBundle"] = authority
        return subprocess.CompletedProcess(arguments, 0, json.dumps(webhook), "")

    def test_first_up_replaces_temporary_leaves_and_exports_only_public_authorities(self) -> None:
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose") as compose,
        ):
            runtime.reconcile_tls(self.root)
            self.assertEqual(compose.call_count, 2)
            self.assertEqual(
                {call.args[-1] for call in compose.call_args_list}, {"floci", "headscale"}
            )
            first_calls = len(self.calls)
            runtime.reconcile_tls(self.root)
            self.assertEqual(compose.call_count, 2)
            self.assertFalse(
                any("apply" in call or "patch" in call for call in self.calls[first_calls:])
            )
        for context, name in (("ctrl", "cell-cluster-ca"), ("cell", "control-cluster-ca")):
            self.assertEqual(set(self.secrets[context, name]["data"]), {"ca.crt"})
        for service in ("headscale", "floci"):
            directory = self.root / ".tmp/state" / service / "tls"
            self.assertEqual((directory / "tls.key").stat().st_mode & 0o777, 0o600)
            self.assertFalse((directory / "ca.key").exists())
        self.assertFalse(any("jsonpath={.data.tls\\.key}" in call for call in self.calls))
        self.assertEqual(
            self.webhooks["ctrl"]["metadata"]["annotations"],
            {
                "cert-manager.io/inject-ca-from": "cert-manager-system/local-floci-tls",
            },
        )
        self.assertEqual(
            self.webhooks["cell"]["metadata"]["annotations"],
            {
                "cert-manager.io/inject-ca-from-secret": "cert-manager-system/control-cluster-ca",
            },
        )
        self.assertEqual(
            self.secrets["cell", "control-cluster-ca"]["metadata"]["annotations"],
            {"cert-manager.io/allow-direct-injection": "true"},
        )

    def test_merged_ca_injection_retires_unrelated_root_and_stays_idempotent(self) -> None:
        self.merge_ca = True
        obsolete = (self.root / ".tmp/state/floci/tls/tls.crt").read_bytes()
        for webhook in self.webhooks.values():
            webhook["webhooks"][0]["clientConfig"]["caBundle"] = base64.b64encode(obsolete).decode()
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose"),
        ):
            runtime.reconcile_tls(self.root)
            first_calls = len(self.calls)
            runtime.reconcile_tls(self.root)
        authority = self.secrets["ctrl", "cluster-local-ca"]["data"]["tls.crt"]
        for webhook in self.webhooks.values():
            self.assertEqual(webhook["webhooks"][0]["clientConfig"]["caBundle"], authority)
        self.assertEqual(sum("--type=json" in call for call in self.calls), 2)
        self.assertFalse(any("patch" in call for call in self.calls[first_calls:]))

    def test_stale_webhook_authority_blocks_export_and_reload(self) -> None:
        self.inject_ca = False
        before = (self.root / ".tmp/state/floci/tls/tls.crt").read_bytes()
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose") as compose,
        ):
            with self.assertRaisesRegex(RuntimeError, "Pod Identity CA injection"):
                runtime.reconcile_tls(self.root, timeout=0)
            compose.assert_not_called()
        self.assertEqual((self.root / ".tmp/state/floci/tls/tls.crt").read_bytes(), before)

    def test_floci_restart_overwrite_must_converge_before_tls_reconcile_returns(self) -> None:
        self.webhooks["ctrl"]["metadata"]["annotations"] = {
            "cert-manager.io/inject-ca-from": "cert-manager-system/local-floci-tls",
        }
        self.webhooks["cell"]["metadata"]["annotations"] = {
            "cert-manager.io/inject-ca-from-secret": "cert-manager-system/control-cluster-ca",
        }

        def restart(*_args: object, **_kwargs: object) -> None:
            self.inject_ca = False
            for webhook in self.webhooks.values():
                webhook["webhooks"][0]["clientConfig"]["caBundle"] = ""

        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose", side_effect=restart),
        ):
            with self.assertRaisesRegex(RuntimeError, "Pod Identity CA injection"):
                runtime.reconcile_tls(self.root, timeout=0)

    def test_foreign_webhook_endpoint_is_not_annotated(self) -> None:
        self.webhooks["ctrl"]["webhooks"][0]["clientConfig"]["url"] = "https://other.invalid/"
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose") as compose,
        ):
            with self.assertRaisesRegex(RuntimeError, "Pod Identity CA injection"):
                runtime.reconcile_tls(self.root, timeout=0)
            compose.assert_not_called()
        self.assertNotIn("annotations", self.webhooks["ctrl"]["metadata"])

    def test_missing_bootstrap_issuer_is_retried_before_exporting_credentials(self) -> None:
        pending = True

        def boundary(
            arguments: list[str], *, stdin: str | None = None, timeout: int = 60
        ) -> subprocess.CompletedProcess[str]:
            nonlocal pending
            if pending and "certificate/cluster-local-ca" in arguments:
                pending = False
                raise RuntimeError("certificate is not created yet")
            return self.boundary(arguments, stdin=stdin, timeout=timeout)

        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=boundary),
            patch.object(runtime, "compose") as compose,
            patch.object(runtime.time, "sleep") as sleep,
        ):
            runtime.reconcile_tls(self.root)
            sleep.assert_called_once_with(2)
            self.assertEqual(compose.call_count, 2)

    def test_invalid_leaf_preserves_both_services_and_credentials(self) -> None:
        before = {
            service: (self.root / ".tmp/state" / service / "tls/tls.crt").read_bytes()
            for service in ("floci", "headscale")
        }
        self.secrets["ctrl", "local-headscale-tls"]["data"]["tls.crt"] = base64.b64encode(
            before["headscale"]
        ).decode()
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose") as compose,
        ):
            with self.assertRaises(RuntimeError):
                runtime.reconcile_tls(self.root)
            compose.assert_not_called()
        for service, content in before.items():
            self.assertEqual(
                (self.root / ".tmp/state" / service / "tls/tls.crt").read_bytes(), content
            )

    def test_floci_leaf_without_configured_ip_is_rejected_before_reload(self) -> None:
        inventory_path = self.root / "src/infra/terraform/deployments/local/deployment.yaml"
        inventory = yaml.safe_load(inventory_path.read_text())
        inventory["network"]["services"]["floci"] = "192.0.2.99"
        inventory_path.write_text(yaml.safe_dump(inventory))
        before = (self.root / ".tmp/state/floci/tls/tls.crt").read_bytes()
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose") as compose,
        ):
            with self.assertRaisesRegex(RuntimeError, "IP address mismatch"):
                runtime.reconcile_tls(self.root)
            compose.assert_not_called()
        self.assertEqual((self.root / ".tmp/state/floci/tls/tls.crt").read_bytes(), before)

    def test_signed_leaf_for_a_different_hostname_is_rejected(self) -> None:
        directory = self.root / ".tmp/state/floci/tls"
        (directory / "tls.crt").write_bytes((directory / "signed.crt").read_bytes())
        (directory / "ca.crt").write_bytes((self.root / "root.crt").read_bytes())
        with self.assertRaisesRegex(RuntimeError, "Hostname mismatch"):
            runtime._validate_runtime_tls(directory, "different.example.invalid")

    def test_failed_reload_is_retried_even_after_files_have_been_installed(self) -> None:
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose", side_effect=RuntimeError("restart failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                runtime.reconcile_tls(self.root)
        with (
            patch.object(runtime, "configuration", return_value=self.fleet),
            patch.object(runtime, "run", side_effect=self.boundary),
            patch.object(runtime, "compose") as compose,
        ):
            runtime.reconcile_tls(self.root)
            self.assertEqual(compose.call_count, 2)

    def test_public_ca_exchange_updates_changed_bundle_and_rejects_unrelated_secrets(self) -> None:
        self.secrets["cell", "control-cluster-ca"] = {
            "data": {"ca.crt": "old-public-bundle"},
            "metadata": {"resourceVersion": "1"},
        }
        with patch.object(runtime, "run", side_effect=self.boundary):
            runtime._reconcile_public_ca(
                {"control": {"record": "ctrl"}, "cells": [{"record": "cell"}]}, timeout=10
            )
            self.assertEqual(
                self.secrets["cell", "control-cluster-ca"]["data"]["ca.crt"],
                self.secrets["ctrl", "cluster-local-ca"]["data"]["tls.crt"],
            )
            self.secrets["cell", "control-cluster-ca"]["metadata"]["ownerReferences"] = [
                {"name": "other-owner"}
            ]
            with self.assertRaisesRegex(RuntimeError, "independently managed"):
                runtime._publish_public_ca("cell", "control-cluster-ca", b"changed public CA")


HANDLED_STORAGE = frozenset((
    ("network", "connect"),
    ("network", "disconnect"),
    ("network", "inspect"),
    ("volume", "rm"),
))


class Docker:
    """In-memory Docker boundary for tests that cannot use a live fleet."""

    def __init__(self, root: Path) -> None:
        self.root = root
        runtime.ensure_floci_tls(root)
        images = root / "src/third_party/k3s-io/k3s/images.toml"
        images.parent.mkdir(parents=True, exist_ok=True)
        images.write_text("", encoding="utf-8")
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, bytes] = {}
        self.networks: set[str] = set()
        self.commands: list[list[str]] = []
        self.stdins: list[str | None] = []
        self.mutations: list[list[str]] = []
        self.fail_build = False
        self.fail_pull = False
        self.fail_registry_connect = False
        self.timeout_registry_connect = False
        self.document = {
            "name": "fixture-compose",
            "services": {
                "floci": {
                    "container_name": "fixture-floci",
                    "environment": {"FLOCI_DOCKER_RESOURCE_NAMESPACE": "fixture"},
                    "networks": {"default": {"ipv4_address": "172.19.0.2"}},
                },
                "git": {
                    "container_name": "fixture-git",
                    "image": "local/git-daemon:2.49.1",
                    "networks": {"default": {"ipv4_address": "172.19.255.21"}},
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(root / ".git"),
                            "target": "/srv/git/openplex.git",
                            "read_only": True,
                            "bind": {"create_host_path": False},
                        }
                    ],
                },
            },
            "networks": {"default": {"name": "fixture-network"}},
            "volumes": {"state": {"name": "fixture-metadata"}},
        }

    def add_fleet(self, *, managed: bool) -> None:
        self.volumes["fixture-metadata"] = b"persisted cloud resource identities"
        self.networks.add("fixture-network")
        self.add_services()
        self.add_parent("old-parent", managed=managed)
        self.add_git("git-id", managed=True)

    def add_services(self) -> None:
        for name, identifier, volume in (
            ("floci-fixture-eks-cell", "cluster-id", "cluster-data"),
            ("floci-fixture-ecr-registry", "registry-id", "registry-data"),
            ("floci-fixture2-eks-cell", "other-cluster-id", "other-cluster-data"),
            ("floci-fixture2-ecr-registry", "other-registry-id", "other-registry-data"),
            ("unrelated", "unrelated-id", "unrelated-data"),
        ):
            self.containers[name] = {
                "Id": identifier,
                "Name": f"/{name}",
                "Mounts": [{"Type": "volume", "Name": volume, "Destination": "/data"}],
                "State": {"Running": True},
                "HostConfig": {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}},
                "Config": {"Labels": {"floci_namespace": "fixture", "io.floci.service": "ecr"}},
                "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.3"}}},
            }
            self.volumes[volume] = f"existing {name} data".encode()
        self.containers["floci-fixture-ecr-registry"]["NetworkSettings"]["Networks"][
            "fixture-network"
        ] = {
            "IPAddress": "172.19.255.22",
            "IPAMConfig": {"IPv4Address": "172.19.255.22"},
            "Aliases": ["origin-registry"],
        }

    def add_parent(self, identifier: str, *, managed: bool) -> None:
        self.containers["fixture-floci"] = {
            "Id": identifier,
            "Name": "/fixture-floci",
            "Config": {
                "Labels": {"com.docker.compose.project": "fixture-compose"} if managed else {},
                "Env": [
                    "FLOCI_DOCKER_RESOURCE_NAMESPACE=fixture",
                    "FLOCI_SERVICES_EKS_KEEP_RUNNING_ON_SHUTDOWN=true",
                    "FLOCI_SERVICES_ECR_KEEP_RUNNING_ON_SHUTDOWN=true",
                    "FLOCI_STORAGE_MODE=persistent",
                    "FLOCI_STORAGE_PRUNE_VOLUMES_ON_DELETE=false",
                ],
            },
            "Mounts": [{"Type": "volume", "Name": "fixture-metadata", "Destination": "/app/data"}],
            "NetworkSettings": {"Networks": {"fixture-network": {}}},
            "State": {"Running": True},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
        }

    def add_git(self, identifier: str, *, managed: bool) -> None:
        self.containers["fixture-git"] = {
            "Id": identifier,
            "Name": "/fixture-git",
            "Config": {
                "Cmd": list(runtime.GIT_DAEMON_COMMAND),
                "Entrypoint": ["git"],
                "Image": "local/git-daemon:2.49.1" if managed else "local/git-daemon",
                "Labels": {
                    "com.docker.compose.project": "fixture-compose",
                    "com.docker.compose.service": "git",
                }
                if managed
                else {},
                "User": runtime.GIT_DAEMON_USER,
            },
            "HostConfig": {
                "AutoRemove": False,
                "CapAdd": None,
                "CapDrop": ["ALL"] if managed else None,
                "NetworkMode": "fixture-network",
                "PortBindings": {"9418/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9418"}]},
                "Privileged": False,
                "ReadonlyRootfs": managed,
                "RestartPolicy": {
                    "Name": "unless-stopped" if managed else "no",
                    "MaximumRetryCount": 0,
                },
                "SecurityOpt": ["no-new-privileges:true"] if managed else None,
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(self.root / ".git"),
                    "Destination": "/srv/git/openplex.git",
                    "RW": False,
                }
            ],
            "NetworkSettings": {
                "Networks": {
                    "fixture-network": {
                        "IPAddress": "172.19.255.21",
                        "IPAMConfig": {"IPv4Address": "172.19.255.21"},
                    }
                }
            },
            "State": {"Health": {"Status": "healthy"}, "Running": True},
        }

    def run(
        self, arguments: list[str], *, input: str | None = None, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(arguments))
        self.stdins.append(input)
        if arguments[0] == "docker-compose":
            return self.compose(arguments)
        output = ""
        operation = arguments[1]
        if operation == "ps":
            output = "\n".join(self.containers)
        elif operation == "inspect":
            output = json.dumps([self.container(name) for name in arguments[2:]])
        elif operation == "build":
            return subprocess.CompletedProcess(
                arguments,
                int(self.fail_build),
                "classic builder failed" if self.fail_build else "candidate built",
                "",
            )
        elif (operation, arguments[2]) in HANDLED_STORAGE:
            handler = self.network if operation == "network" else self.remove_volumes
            return handler(arguments)
        elif operation in {"volume", "network"}:
            output = self.storage(arguments)
        elif operation in {"start", "stop", "update"}:
            return self.container_lifecycle(arguments)
        elif operation == "rm":
            self.mutations.append(list(arguments))
            for name in arguments[2:]:
                del self.containers[self.container(name)["Name"].removeprefix("/")]
        elif operation == "exec":
            return subprocess.CompletedProcess(
                arguments, int(not self.container(arguments[2])["State"]["Running"]), "", ""
            )
        else:
            raise ValueError(f"Unexpected Docker operation: {arguments}")
        return subprocess.CompletedProcess(arguments, 0, output, "")

    def container_lifecycle(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        self.mutations.append(list(arguments))
        operation = arguments[1]
        if operation == "update":
            for name in arguments[4:]:
                self.container(name)["HostConfig"]["RestartPolicy"]["Name"] = arguments[3]
        else:
            for name in arguments[4:] if operation == "stop" else arguments[2:]:
                self.container(name)["State"]["Running"] = operation == "start"
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def network(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if arguments[2] == "inspect":
            endpoints = {
                container["Id"]: {"IPv4Address": f"{endpoint['IPAddress']}/16"}
                for container in self.containers.values()
                if (endpoint := container["NetworkSettings"]["Networks"].get(arguments[-1]))
            }
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps([{"Containers": endpoints}]), ""
            )
        self.mutations.append(list(arguments))
        networks = self.container(arguments[-1])["NetworkSettings"]["Networks"]
        network = arguments[-2]
        if arguments[2] == "disconnect":
            del networks[network]
            return subprocess.CompletedProcess(arguments, 0, "", "")
        options = list(zip(arguments[3:-2:2], arguments[4:-2:2], strict=True))
        address = next((value for flag, value in options if flag == "--ip"), None)
        aliases = [value for flag, value in options if flag == "--alias"]
        if self.fail_registry_connect and address == "172.19.255.22":
            return subprocess.CompletedProcess(arguments, 1, "", "endpoint unavailable")
        networks[network] = {
            "IPAddress": (address or "172.19.0.3")
            if self.container(arguments[-1])["State"]["Running"]
            else "",
            "IPAMConfig": {"IPv4Address": address} if address else None,
            "Aliases": aliases or None,
        }
        if self.timeout_registry_connect and address == "172.19.255.22":
            raise subprocess.TimeoutExpired(arguments, 60)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def compose(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if "config" in arguments:
            return subprocess.CompletedProcess(arguments, 0, json.dumps(self.document), "")
        if "pull" in arguments:
            return subprocess.CompletedProcess(arguments, int(self.fail_pull), "", "")
        if "up" not in arguments:
            raise ValueError(f"Unexpected Compose operation: {arguments}")
        if "fixture-metadata" not in self.volumes or "fixture-network" not in self.networks:
            return subprocess.CompletedProcess(arguments, 1, "", "external storage missing")
        self.mutations.append(list(arguments))
        if "fixture-floci" not in self.containers:
            self.add_parent("compose-parent", managed=True)
        self.containers["fixture-floci"]["State"]["Running"] = True
        if "fixture-git" not in self.containers:
            self.add_git("compose-git", managed=True)
        self.containers["fixture-git"]["State"] = {
            "Health": {"Status": "healthy"},
            "Running": True,
        }
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def storage(self, arguments: list[str]) -> str:
        collection = self.volumes if arguments[1] == "volume" else self.networks
        if arguments[2] == "ls":
            return "\n".join(collection)
        if arguments[2] != "create":
            raise ValueError(f"Unexpected storage operation: {arguments}")
        self.mutations.append(list(arguments))
        if arguments[1] == "volume":
            self.volumes[arguments[-1]] = b""
        else:
            self.networks.add(arguments[-1])
        return ""

    def remove_volumes(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        used = {
            mount["Name"]
            for container in self.containers.values()
            for mount in container["Mounts"]
            if mount["Type"] == "volume"
        }
        if used.intersection(arguments[3:]):
            return subprocess.CompletedProcess(arguments, 1, "", "volume is in use")
        self.mutations.append(list(arguments))
        for name in arguments[3:]:
            del self.volumes[name]
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def container(self, identifier: str) -> dict[str, Any]:
        for container in self.containers.values():
            if identifier in {
                container["Id"],
                container["Name"],
                container["Name"].removeprefix("/"),
            }:
                return container
        raise ValueError(f"Unknown container: {identifier}")


if __name__ == "__main__":
    unittest.main()
