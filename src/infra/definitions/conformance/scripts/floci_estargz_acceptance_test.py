#!/usr/bin/env python3
"""Tests unattended Floci eStargz acceptance contracts and fail-closed image routing validations offline to defend against faulty cluster verification passes."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import signal
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import types
    from collections.abc import Callable

SCRIPT_DIRECTORY = pathlib.Path(__file__).resolve().parent
PROGRAM = SCRIPT_DIRECTORY / "floci_estargz_acceptance.py"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
SOURCE_REVISION = "1" * 40
STREAM_TAG = "20260901T000000Z_" + SOURCE_REVISION[:12]
FIXTURE_INSTALLATION = "fixture"


def load_subject() -> types.ModuleType:
    specification = importlib.util.spec_from_file_location("floci_estargz_acceptance", PROGRAM)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot import {PROGRAM}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


subject = load_subject()
TOPOLOGY = subject.LocalTopology(
    installation=FIXTURE_INSTALLATION,
    control="ctrl-eaws-lh1",
    cells=("cell-eaws-lh1",),
    storage_writer_cell="cell-eaws-lh1",
)
ACCEPTANCE_CHILD = f"floci-{FIXTURE_INSTALLATION}-local-cell-eaws-lh1-eks-cell-eaws-lh1"


def manifest(*layers: str) -> str:
    return json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": DIGEST_D,
            "size": 2,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": digest,
                "size": 100,
                "annotations": {
                    "containerd.io/snapshot/stargz/toc.digest": DIGEST_D,
                },
            }
            for digest in layers
        ],
    })


def config(architecture: str = "arm64") -> str:
    return json.dumps({"architecture": architecture, "os": "linux"})


def reference(character: str, authority: str = "localhost:15100") -> str:
    return f"{authority}/000000000000/us-west-2/src/examples/ray_train@sha256:{character * 64}"


def repository_reference(character: str, repository: str) -> str:
    return (
        f"localhost:15100/000000000000/us-west-2/src/examples/{repository}@sha256:{character * 64}"
    )


def publication_payload(**overrides: object) -> str:
    document: dict[str, object] = {
        "version": 1,
        "sourceRevision": SOURCE_REVISION,
        "streamTag": STREAM_TAG,
        "topology": {
            "installation": FIXTURE_INSTALLATION,
            "control": "ctrl-eaws-lh1",
            "cells": ["cell-eaws-lh1"],
            "storageWriterCell": "cell-eaws-lh1",
        },
        "acceptanceChild": ACCEPTANCE_CHILD,
        "images": [
            repository_reference("1", "ray_data"),
            repository_reference("2", "ray_train"),
            repository_reference("3", "svelte_web"),
        ],
    }
    document.update(overrides)
    return json.dumps(document)


def image(character: str, target: str) -> object:
    return subject.parse_image_contract(
        reference(character), target, manifest(target), config(), "arm64"
    )


class LocalFlociEstargzAcceptanceTest(unittest.TestCase):
    def test_derives_every_floci_child_from_local_topology(self) -> None:
        topology = subject.parse_local_topology(
            json.dumps({
                "installation": FIXTURE_INSTALLATION,
                "control": "ctrl-eaws-lh1",
                "cells": ["cell-eaws-lh1"],
                "storageWriterCell": "cell-eaws-lh1",
            })
        )

        self.assertEqual(topology, TOPOLOGY)
        self.assertEqual(
            topology.cluster_children,
            {
                "ctrl-eaws-lh1": (f"floci-{FIXTURE_INSTALLATION}-local-eks-ctrl-eaws-lh1"),
                "cell-eaws-lh1": ACCEPTANCE_CHILD,
            },
        )
        self.assertEqual(topology.acceptance_child, ACCEPTANCE_CHILD)

        with self.assertRaises(subject.AcceptanceError):
            subject.parse_local_topology(
                json.dumps({
                    "installation": FIXTURE_INSTALLATION,
                    "control": "ctrl-eaws-lh1",
                    "cells": ["cell-eaws-lh1"],
                    "storageWriterCell": "cell-eaws-other",
                })
            )

    def test_accepts_only_owned_digest_references(self) -> None:
        authority, repository, digest = subject.parse_image_reference(reference("1"))
        self.assertEqual(authority, "localhost:15100")
        self.assertEqual(repository, "000000000000/us-west-2/src/examples/ray_train")
        self.assertEqual(digest, "sha256:" + "1" * 64)
        self.assertEqual(
            subject.parse_image_reference(reference("2", "127.0.0.1:15100"))[0],
            "127.0.0.1:15100",
        )

        for rejected in (
            "localhost:15100/src/examples/ray_train:latest",
            "localhost:15100/src/../ray_train@sha256:" + "1" * 64,
            "registry.example.com/src/examples/ray_train@sha256:" + "1" * 64,
            "http://localhost:15100/src/examples/ray_train@sha256:" + "1" * 64,
        ):
            with (
                self.subTest(rejected=rejected),
                self.assertRaises(subject.AcceptanceError),
            ):
                subject.parse_image_reference(rejected)

    def test_parses_only_complete_local_up_publication_contracts(self) -> None:
        contract = subject.parse_publication_contract(publication_payload())

        self.assertEqual(contract.source_revision, SOURCE_REVISION)
        self.assertEqual(contract.stream_tag, STREAM_TAG)
        self.assertEqual(contract.topology, TOPOLOGY)
        self.assertEqual(contract.acceptance_child, ACCEPTANCE_CHILD)
        self.assertEqual(len(contract.image_references), 3)

        rejected = (
            {"streamTag": "20260901T000000Z_222222222222"},
            {"acceptanceChild": "floci-foreign"},
            {"images": [repository_reference("1", "ray_data")] * 3},
            {
                "images": [
                    repository_reference("1", "ray_data"),
                    repository_reference("2", "ray_train"),
                    reference("3", "127.0.0.1:15100"),
                ]
            },
        )
        for override in rejected:
            with (
                self.subTest(override=override),
                self.assertRaises(subject.AcceptanceError),
            ):
                subject.parse_publication_contract(publication_payload(**override))

    def test_loads_only_a_regular_completed_local_up_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / ".tmp/state"
            state.mkdir(parents=True)
            contract_path = root / subject.PUBLICATION_CONTRACT_PATH
            contract_path.write_text(publication_payload())
            self.assertEqual(subject.load_publication_contract(root).topology, TOPOLOGY)

            contract_path.unlink()
            contract_path.symlink_to(root / "missing")
            with self.assertRaises(subject.AcceptanceError):
                subject.load_publication_contract(root)

    def test_refuses_stale_publication_context_before_state_changes(self) -> None:
        publication = subject.parse_publication_contract(publication_payload())
        subject.require_publication_context(publication, TOPOLOGY, SOURCE_REVISION)

        rejected = (
            (publication, TOPOLOGY, "2" * 40),
            (
                publication,
                subject.dataclasses.replace(TOPOLOGY, storage_writer_cell="cell-eaws-other"),
                SOURCE_REVISION,
            ),
        )
        for candidate, topology, revision in rejected:
            with (
                self.subTest(topology=topology, revision=revision),
                self.assertRaises(subject.AcceptanceError),
            ):
                subject.require_publication_context(candidate, topology, revision)

    def test_selects_the_exact_node_manifest_from_an_image_index(self) -> None:
        index = json.dumps({
            "schemaVersion": 2,
            "mediaType": subject.OCI_IMAGE_INDEX_MEDIA_TYPE,
            "manifests": [
                {
                    "mediaType": subject.OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                    "digest": DIGEST_A,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "mediaType": subject.OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                    "digest": DIGEST_B,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ],
        })

        self.assertEqual(
            subject.platform_manifest_reference(reference("1"), index, "arm64"),
            reference("b"),
        )
        with self.assertRaises(subject.AcceptanceError):
            subject.platform_manifest_reference(reference("1"), index, "s390x")

    def test_requires_single_platform_estargz_and_a_target_layer(self) -> None:
        accepted = subject.parse_image_contract(
            reference("1"), DIGEST_A, manifest(DIGEST_A, DIGEST_B), config(), "arm64"
        )
        self.assertEqual(accepted.target_layer, DIGEST_A)

        bad_layer = json.loads(manifest(DIGEST_A))
        bad_layer["layers"][0]["annotations"] = {}
        rejected = (
            (
                json.dumps({
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [],
                }),
                config(),
                DIGEST_A,
            ),
            (json.dumps(bad_layer), config(), DIGEST_A),
            (manifest(DIGEST_A), config("amd64"), DIGEST_A),
            (manifest(DIGEST_A), config(), DIGEST_B),
        )
        for manifest_payload, config_payload, target in rejected:
            with (
                self.subTest(manifest=manifest_payload, target=target),
                self.assertRaises(subject.AcceptanceError),
            ):
                subject.parse_image_contract(
                    reference("1"),
                    target,
                    manifest_payload,
                    config_payload,
                    "arm64",
                )

    def test_requires_distinct_target_layers(self) -> None:
        subject.require_distinct_target_layers([image("1", DIGEST_A), image("2", DIGEST_B)])
        with self.assertRaises(subject.AcceptanceError):
            subject.require_distinct_target_layers([image("1", DIGEST_A), image("2", DIGEST_A)])

    def test_selects_three_cold_layers_without_future_pull_overlap(self) -> None:
        images = (
            subject.PublishedImage(
                repository_reference("1", "ray_data"),
                subject.CANONICAL_AUTHORITY,
                (DIGEST_A, DIGEST_B),
            ),
            subject.PublishedImage(
                repository_reference("2", "ray_train"),
                subject.CANONICAL_AUTHORITY,
                (DIGEST_B, DIGEST_C),
            ),
            subject.PublishedImage(
                repository_reference("3", "ray_serve"),
                subject.CANONICAL_AUTHORITY,
                (DIGEST_D,),
            ),
            subject.PublishedImage(
                repository_reference("4", "svelte_web"),
                subject.CANONICAL_AUTHORITY,
                ("sha256:" + "e" * 64,),
            ),
        )

        selected = subject.select_cold_images(
            images,
            {DIGEST_A},
            set(),
            frozenset(),
        )

        self.assertEqual(
            [candidate.target_layer for candidate in selected],
            [DIGEST_B, DIGEST_C, DIGEST_D],
        )
        self.assertEqual(
            [candidate.authority for candidate in selected],
            [
                subject.CANONICAL_AUTHORITY,
                subject.ACCEPTANCE_AUTHORITY,
                subject.ACCEPTANCE_AUTHORITY,
            ],
        )
        with self.assertRaises(subject.AcceptanceError):
            subject.select_cold_images(images[:2], set(), set(), frozenset())

    def test_sums_labeled_and_unlabeled_download_counters(self) -> None:
        metrics = "\n".join((
            "# HELP dragonfly_client_download_task_total tasks",
            'dragonfly_client_download_task_total{type="0"} 2',
            "dragonfly_client_download_task_total 3",
        ))
        self.assertEqual(subject.parse_download_tasks(metrics), 5)
        self.assertEqual(subject.parse_download_tasks("unrelated_total 5\n"), 0)
        with self.assertRaisesRegex(subject.AcceptanceError, "is invalid"):
            subject.parse_download_tasks("dragonfly_client_download_task_total broken\n")

    def test_requires_rendered_registry_hosts_to_match_source(self) -> None:
        source = """\
server = "http://origin-registry:5000/v2"
[host."http://127.0.0.1:4001"]
capabilities = ["pull", "resolve"]
[host."http://127.0.0.1:4001".header]
X-Dragonfly-Registry = ["http://origin-registry:5000"]
"""
        rendered = source.replace("server =", "# generated\nserver =")
        subject.require_registry_hosts_source(rendered, source, TOPOLOGY.control)

        obsolete = rendered.replace("origin-registry", "floci-fixture-local-ecr-registry")
        with self.assertRaisesRegex(subject.AcceptanceError, "differ from source"):
            subject.require_registry_hosts_source(obsolete, source, TOPOLOGY.control)

    def test_parses_snapshot_tasks_and_listening_ports(self) -> None:
        snapshots = "KEY PARENT KIND\nlayer-a  Committed\nlayer-b layer-a Committed\n"
        self.assertEqual(subject.parse_snapshot_keys(snapshots), frozenset({"layer-a", "layer-b"}))
        tasks = f"ID SIZE\n{DIGEST_A.removeprefix('sha256:')} 10 MB\n"
        self.assertEqual(
            subject.parse_task_ids(tasks),
            frozenset({DIGEST_A.removeprefix("sha256:")}),
        )
        sockets = (
            "sl local_address rem_address st\n"
            "0: 0100007F:0001 00000000:0000 0A\n"
            "1: 0100007F:0FA1 00000000:0000 0A\n"
        )
        self.assertEqual(subject.listening_ports(sockets), frozenset({1, 4001}))

    def test_requires_embedded_k3s_process_and_containerd_config(self) -> None:
        process_table = "PID COMMAND\n1 /bin/k3s init\n313 {exe} k3s server\n"
        subject.validate_k3s_process_model("/bin/k3s init\0", process_table)
        containerd_config = f"""
version = 3

[plugins.'io.containerd.cri.v1.images']
snapshotter = "stargz"
disable_snapshot_annotations = false
use_local_image_pull = true

[plugins.'io.containerd.snapshotter.v1.stargz'.registry]
config_path = "{subject.REGISTRY_HOSTS_ROOT}"
"""
        subject.validate_containerd_config(containerd_config)
        with self.assertRaises(subject.AcceptanceError):
            subject.validate_k3s_process_model("/bin/k3s server\0", process_table)
        with self.assertRaises(subject.AcceptanceError):
            subject.validate_k3s_process_model(
                "/bin/k3s init\0",
                "PID COMMAND\n1 /bin/k3s init\n",
            )
        with self.assertRaises(subject.AcceptanceError):
            subject.validate_containerd_config(
                containerd_config + '\n[proxy_plugins.stargz]\ntype = "snapshot"\n'
            )
        with self.assertRaises(subject.AcceptanceError):
            subject.validate_containerd_config(
                containerd_config.replace('snapshotter = "stargz"', 'snapshotter = "overlayfs"')
            )

    def test_rejects_ambiguous_or_external_stargz_processes(self) -> None:
        for processes in (
            ("PID COMMAND\n1 /bin/k3s init\n313 {exe} k3s server\n414 {exe} k3s server\n"),
            (
                "PID COMMAND\n"
                "1 /bin/k3s init\n"
                "313 {exe} k3s server\n"
                "414 containerd-stargz-grpc --config /etc/test\n"
            ),
        ):
            with (
                self.subTest(processes=processes),
                self.assertRaises(subject.AcceptanceError),
            ):
                subject.validate_k3s_process_model("/bin/k3s init\0", processes)

    def test_requires_ready_node_without_custom_runtime_state(self) -> None:
        node = {
            "items": [
                {
                    "metadata": {
                        "name": "cell-eaws-lh1",
                        "labels": {
                            "kubernetes.io/arch": "arm64",
                        },
                    },
                    "spec": {"taints": []},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            ]
        }
        self.assertEqual(
            subject.validate_node(json.dumps(node), "cell-eaws-lh1"),
            ("cell-eaws-lh1", "arm64"),
        )
        node["items"][0]["metadata"]["labels"]["example.invalid/stargz-runtime"] = "embedded"
        with self.assertRaises(subject.AcceptanceError):
            subject.validate_node(json.dumps(node), "cell-eaws-lh1")

    def test_inspects_embedded_stargz_everywhere_and_clients_only_on_cells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            acceptance = subject.Acceptance(subject.SubprocessRunner(), root, TOPOLOGY)
            canonical_hosts = 'server = "http://origin-registry:5000/v2"\n'
            source_hosts = root / subject.SOURCE_HOSTS_PATH
            source_hosts.parent.mkdir(parents=True)
            source_hosts.write_text(canonical_hosts)
            rendered_hosts = {
                TOPOLOGY.control: canonical_hosts,
                TOPOLOGY.cells[0]: canonical_hosts,
            }
            for cluster, contents in rendered_hosts.items():
                path = acceptance.rendered_hosts_path(cluster)
                path.parent.mkdir(parents=True)
                path.write_text(contents)

            inspected_children: list[tuple[str, str]] = []
            kubectl_calls: list[tuple[str, tuple[str, ...]]] = []
            control_has_client = False

            acceptance.inspect_child = lambda child, canonical_hosts: inspected_children.append((
                child,
                canonical_hosts,
            ))

            def kubectl(
                cluster: str,
                arguments: list[str],
                *,
                check: bool = True,
                timeout_seconds: int = subject.COMMAND_TIMEOUT_SECONDS,
            ) -> subprocess.CompletedProcess[str]:
                del check, timeout_seconds
                kubectl_calls.append((cluster, tuple(arguments)))
                if arguments == ["get", "nodes", "-o", "json"]:
                    payload = {
                        "items": [
                            {
                                "metadata": {
                                    "name": cluster,
                                    "labels": {"kubernetes.io/arch": "arm64"},
                                },
                                "spec": {"taints": []},
                                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                            }
                        ]
                    }
                elif arguments == [
                    "-n",
                    "dragonfly-system",
                    "get",
                    "daemonsets",
                    "-o",
                    "json",
                ]:
                    items = [{"metadata": {"name": "dragonfly-manager"}}]
                    if control_has_client:
                        items.append({"metadata": {"name": subject.CLIENT_DAEMONSET}})
                    payload = {"items": items}
                elif arguments == [
                    "-n",
                    "dragonfly-system",
                    "get",
                    "daemonset",
                    subject.CLIENT_DAEMONSET,
                    "-o",
                    "json",
                ]:
                    payload = {
                        "spec": {
                            "template": {"spec": {"nodeSelector": {"kubernetes.io/os": "linux"}}}
                        },
                        "status": {"desiredNumberScheduled": 1, "numberReady": 1},
                    }
                else:
                    self.fail(f"unexpected kubectl call: {cluster} {arguments}")
                return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

            acceptance.kubectl = kubectl
            acceptance.observe_client = lambda cluster: subject.ClientObservation(
                node=cluster,
                pod=f"{cluster}-client",
                pod_uid=f"{cluster}-uid",
                restart_count=0,
                download_tasks=0,
                task_ids=frozenset(),
            )

            acceptance.inspect()

            self.assertEqual(
                inspected_children,
                [
                    (TOPOLOGY.cluster_children[cluster], rendered_hosts[cluster])
                    for cluster in TOPOLOGY.cluster_children
                ],
            )
            self.assertEqual(set(acceptance.node_names), set(TOPOLOGY.cluster_children))
            self.assertEqual(set(acceptance.clients), set(TOPOLOGY.cells))
            self.assertIn(
                (
                    TOPOLOGY.control,
                    ("-n", "dragonfly-system", "get", "daemonsets", "-o", "json"),
                ),
                kubectl_calls,
            )

            control_has_client = True
            with self.assertRaisesRegex(
                subject.AcceptanceError,
                "must not run the cell-only Dragonfly client",
            ):
                acceptance.inspect()

    def test_requires_remote_snapshot_for_the_target_layer(self) -> None:
        snapshot = {
            "Labels": {
                "containerd.io/snapshot/remote": "remote snapshot",
                "containerd.io/snapshot/remote/stargz.digest": DIGEST_A,
            }
        }
        self.assertEqual(subject.snapshot_target_digest(json.dumps(snapshot)), DIGEST_A)
        snapshot["Labels"]["containerd.io/snapshot/remote"] = "ordinary"
        self.assertIsNone(subject.snapshot_target_digest(json.dumps(snapshot)))

    def test_reads_blob_task_identity_from_effective_yaml(self) -> None:
        config_payload = """
proxy:
  server:
    ip: 127.0.0.1
  registryMirror:
    addr: https://index.docker.io
    enableTaskIDBasedBlobDigest: true
"""
        self.assertEqual(
            subject.yaml_scalar(
                config_payload,
                ("proxy", "registryMirror", "enableTaskIDBasedBlobDigest"),
            ),
            "true",
        )
        self.assertIsNone(subject.yaml_scalar(config_payload, ("proxy", "missing")))

    def test_restores_acceptance_hosts_after_failure_and_signal(self) -> None:
        calls: list[str] = []
        with self.assertRaises(RuntimeError):
            with subject.temporary_fallback_route(calls.append, "normal", "fallback"):
                raise RuntimeError("pull failed")
        self.assertEqual(calls, ["normal", "fallback", "normal"])

        calls.clear()
        with self.assertRaises(subject.AcceptanceInterrupted):
            with subject.temporary_fallback_route(calls.append, "normal", "fallback"):
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                cast("Callable[[int, object], object]", handler)(signal.SIGTERM, None)
        self.assertEqual(calls, ["normal", "fallback", "normal"])

    def test_restores_acceptance_hosts_when_fallback_install_fails(self) -> None:
        calls: list[str] = []

        def install(contents: str) -> None:
            calls.append(contents)
            if contents == "fallback":
                raise RuntimeError("install failed")

        with self.assertRaises(RuntimeError):
            with subject.temporary_fallback_route(install, "normal", "fallback"):
                self.fail("fallback installation unexpectedly succeeded")
        self.assertEqual(calls, ["normal", "fallback", "normal"])

    def test_subprocess_deadlines_fail_closed(self) -> None:
        with unittest.mock.patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["oras", "manifest", "fetch"], 7),
        ):
            with self.assertRaisesRegex(subject.AcceptanceError, "7s deadline"):
                subject.SubprocessRunner().run(["oras", "manifest", "fetch"], timeout_seconds=7)

        with self.assertRaises(ValueError):
            subject.SubprocessRunner().run(["true"], timeout_seconds=0)

    def test_cri_pull_uses_direct_dispatch_and_the_extended_bounded_deadline(self) -> None:
        class RecordingRunner:
            def __init__(self) -> None:
                self.timeouts: list[int] = []
                self.arguments: list[list[str]] = []

            def run(
                self,
                arguments: list[str],
                *,
                input_text: str | None = None,
                check: bool = True,
                timeout_seconds: int,
            ) -> subprocess.CompletedProcess[str]:
                del input_text, check
                self.timeouts.append(timeout_seconds)
                self.arguments.append(arguments)
                return subprocess.CompletedProcess(arguments, 0, "", "")

        runner = RecordingRunner()
        acceptance = subject.Acceptance(runner, pathlib.Path("/unused"), TOPOLOGY)
        before = subject.ClientObservation(
            node="node",
            pod="client",
            pod_uid="uid",
            restart_count=0,
            download_tasks=1,
            task_ids=frozenset(),
        )
        after = subject.dataclasses.replace(
            before,
            download_tasks=2,
            task_ids=frozenset({DIGEST_A.removeprefix("sha256:")}),
        )
        observations = iter((before, after))
        acceptance.observe_client = lambda _cluster: next(observations)
        acceptance.snapshot_keys = frozenset
        acceptance.require_new_remote_snapshot = lambda _before, _image: None

        acceptance.pull(image("1", DIGEST_A), route="dragonfly", expected_client_uid="uid")

        self.assertEqual(runner.timeouts, [subject.PULL_TIMEOUT_SECONDS])
        self.assertEqual(
            runner.arguments,
            [
                [
                    "docker",
                    "--context",
                    "colima",
                    "exec",
                    ACCEPTANCE_CHILD,
                    "crictl",
                    "pull",
                    reference("1"),
                ]
            ],
        )

    def test_containerd_inspection_uses_direct_ctr_dispatch(self) -> None:
        class RecordingRunner:
            def __init__(self) -> None:
                self.arguments: list[list[str]] = []

            def run(
                self,
                arguments: list[str],
                *,
                input_text: str | None = None,
                check: bool = True,
                timeout_seconds: int,
            ) -> subprocess.CompletedProcess[str]:
                del input_text, check, timeout_seconds
                self.arguments.append(arguments)
                if arguments[-1] == "ls":
                    payload = "KEY PARENT KIND\n"
                elif arguments[-2:] == ["info", "snapshot-key"]:
                    payload = json.dumps({"Labels": {}})
                else:
                    raise AssertionError(f"unexpected containerd command: {arguments}")
                return subprocess.CompletedProcess(arguments, 0, payload, "")

        runner = RecordingRunner()
        acceptance = subject.Acceptance(runner, pathlib.Path("/unused"), TOPOLOGY)

        self.assertEqual(acceptance.snapshot_keys(), frozenset())
        self.assertIsNone(acceptance.snapshot_digest("snapshot-key"))
        self.assertEqual(
            [arguments[5] for arguments in runner.arguments],
            ["ctr", "ctr"],
        )

    def test_refuses_a_symlinked_acceptance_hosts_path(self) -> None:
        class SymlinkRunner:
            def run(
                self,
                arguments: list[str],
                *,
                input_text: str | None = None,
                check: bool = True,
                timeout_seconds: int,
            ) -> subprocess.CompletedProcess[str]:
                del input_text, check, timeout_seconds
                if arguments[-4:] == [
                    "test",
                    "!",
                    "-L",
                    subject.ACCEPTANCE_HOSTS_PATH,
                ]:
                    raise subject.AcceptanceError("acceptance hosts is a symlink")
                return subprocess.CompletedProcess(arguments, 0, "", "")

        acceptance = subject.Acceptance(SymlinkRunner(), pathlib.Path("/unused"), TOPOLOGY)
        with self.assertRaises(subject.AcceptanceError):
            acceptance.require_owned_hosts_path(ACCEPTANCE_CHILD, subject.ACCEPTANCE_HOSTS_PATH)

    def test_refuses_rendered_runtime_drift_before_repairing_retained_state(self) -> None:
        class ReadOnlyRunner:
            def __init__(self) -> None:
                self.arguments: list[list[str]] = []

            def run(
                self,
                arguments: list[str],
                *,
                input_text: str | None = None,
                check: bool = True,
                timeout_seconds: int,
            ) -> subprocess.CompletedProcess[str]:
                del input_text, check
                self.assert_timeout(timeout_seconds)
                self.arguments.append(arguments)
                return subprocess.CompletedProcess(arguments, 0, "immutable\n", "")

            @staticmethod
            def assert_timeout(timeout_seconds: int) -> None:
                if timeout_seconds != subject.COMMAND_TIMEOUT_SECONDS:
                    raise AssertionError("command did not use the bounded default deadline")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            acceptance = subject.Acceptance(subject.SubprocessRunner(), root, TOPOLOGY)
            rendered = acceptance.rendered_hosts_path(TOPOLOGY.storage_writer_cell)
            rendered.parent.mkdir(parents=True)
            source_hosts = root / subject.SOURCE_HOSTS_PATH
            source_hosts.parent.mkdir(parents=True)
            source_hosts.write_text('server = "http://origin-registry:5000/v2"\n')

            with self.subTest("rendered contract differs from runtime"):
                rendered.write_text(source_hosts.read_text())
                runner = ReadOnlyRunner()
                acceptance = subject.Acceptance(runner, root, TOPOLOGY)
                with self.assertRaises(subject.AcceptanceError):
                    acceptance.prepare_pulls()
                self.assertEqual(len(runner.arguments), 1)
                self.assertEqual(runner.arguments[0][-2:], ["cat", subject.BAKED_HOSTS_PATH])

            with self.subTest("missing rendered contract"):
                rendered.unlink()
                runner = ReadOnlyRunner()
                acceptance = subject.Acceptance(runner, root, TOPOLOGY)
                with self.assertRaises(subject.AcceptanceError):
                    acceptance.prepare_pulls()
                self.assertEqual(runner.arguments, [])

    def test_repairs_owned_hosts_before_strict_inspection(self) -> None:
        events: list[str] = []
        acceptance = subject.Acceptance(
            subject.SubprocessRunner(), pathlib.Path("/unused"), TOPOLOGY
        )
        acceptance.verified_normal_hosts = lambda: events.append("verify") or "normal"
        acceptance.install_acceptance_hosts = lambda _contents: events.append("repair")
        acceptance.inspect = lambda: events.append("inspect")

        self.assertEqual(acceptance.prepare_pulls(), "normal")
        self.assertEqual(events, ["verify", "repair", "inspect"])

    def test_has_no_runtime_path_or_restart_override(self) -> None:
        help_text = subject.argument_parser().format_help()
        for rejected in (
            "hosts-path",
            "restart",
            "interrupt",
            "proxy-image",
            "expected-client-uid",
            "accept-persistent-state",
        ):
            self.assertNotIn(rejected, help_text)
        options = subject.argument_parser().parse_args(["pulls"])
        self.assertEqual(options.mode, "pulls")
        self.assertEqual(vars(options), {"mode": "pulls"})


if __name__ == "__main__":
    unittest.main()
