#!/usr/bin/env python3
"""Unit tests verifying command generation, credential routing, and argument parsing in the workload CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, override
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import Iterator


class ExpectedSystemExitError(AssertionError):
    """SystemExit was not raised as expected."""


class UnexpectedSystemExitCodeError(AssertionError):
    """SystemExit was raised with an unexpected exit code."""


@contextmanager
def raises_system_exit(expected_code: int | None = None) -> Iterator[None]:
    """Assert that a block raises SystemExit with an optional exit code."""
    caught: SystemExit | None = None
    try:
        yield
    except SystemExit as err:
        caught = err
    if caught is None:
        raise ExpectedSystemExitError
    if expected_code is not None and caught.code != expected_code:
        raise UnexpectedSystemExitCodeError


try:
    from src.bazel.rules.oci.workload_cli import (
        EXTRA_TAG_PATTERN,
        ImageDelivery,
        MutationConfig,
        PublishArgs,
        RunArgs,
        build_ecr_environment,
        check_ray_dashboard,
        deliver_to_ecr,
        deliver_to_local_registry,
        determine_publish_repository,
        determine_stream_tag,
        execute_publish,
        execute_run,
        main,
        mutate_manifest,
        parse_extra_tags,
        parse_publish_images,
        validate_ecr_identity,
        validate_run_preflight,
    )
except ImportError:
    from workload_cli import (
        EXTRA_TAG_PATTERN,
        ImageDelivery,
        MutationConfig,
        PublishArgs,
        RunArgs,
        build_ecr_environment,
        check_ray_dashboard,
        deliver_to_ecr,
        deliver_to_local_registry,
        determine_publish_repository,
        determine_stream_tag,
        execute_publish,
        execute_run,
        main,
        mutate_manifest,
        parse_extra_tags,
        parse_publish_images,
        validate_ecr_identity,
        validate_run_preflight,
    )


class TestValidationFunctions(unittest.TestCase):
    """Test argument validation, naming constraints, and security checks."""

    @staticmethod
    def test_run_name_and_kuberay_47_char_limit() -> None:
        args = RunArgs(
            workload="ray-train",
            repository_path="src/examples/ray_train",
            publisher="publisher",
            manifest="manifest.yaml",
            team_namespace="team-examples",
            kubectl="kubectl",
        )
        env = {
            "WORKLOAD_RUN_ID": "run123",
            "WORKLOAD_LAUNCHER": "alice",
            "WORKLOAD_TARGET_CELL": "cell-eaws-lh1",
        }
        with patch.dict(os.environ, env, clear=True):
            run_id, launcher, run_name, target_cell = validate_run_preflight(args)
            assert run_id == "run123"
            assert launcher == "alice"
            assert run_name == "ray-train-alice-run123"
            assert target_cell == "cell-eaws-lh1"

        # Name exceeding 47 characters fails
        long_env = {
            "WORKLOAD_RUN_ID": "verylongrunid123",  # 16 chars -> invalid
            "WORKLOAD_LAUNCHER": "alice",
            "WORKLOAD_TARGET_CELL": "cell-eaws-lh1",
        }
        with patch.dict(os.environ, long_env, clear=True), raises_system_exit():
            validate_run_preflight(args)

    @staticmethod
    def test_missing_run_id_defaults_to_unix_seconds() -> None:
        args = RunArgs("ray", "src/ray", "pub", "man", "team-ex", "k")
        with (
            patch.dict(
                os.environ,
                {"WORKLOAD_TARGET_CELL": "cell-eaws-lh1", "WORKLOAD_LAUNCHER": "alice"},
                clear=True,
            ),
            patch("time.time", return_value=1700000000.0),
        ):
            run_id, launcher, run_name, target_cell = validate_run_preflight(args)
            assert run_id == "1700000000"
            assert launcher == "alice"
            assert run_name == "ray-alice-1700000000"
            assert target_cell == "cell-eaws-lh1"

    @staticmethod
    def test_invalid_target_cell_fails() -> None:
        args = RunArgs("ray", "src/ray", "pub", "man", "team-ex", "k")
        with (
            patch.dict(
                os.environ,
                {"WORKLOAD_RUN_ID": "r1", "WORKLOAD_TARGET_CELL": "invalid-cell"},
                clear=True,
            ),
            raises_system_exit(),
        ):
            validate_run_preflight(args)

    @staticmethod
    def test_invalid_team_namespace_fails() -> None:
        args = RunArgs("ray", "src/ray", "pub", "man", "invalid_team", "k")
        with (
            patch.dict(
                os.environ,
                {"WORKLOAD_RUN_ID": "r1", "WORKLOAD_TARGET_CELL": "cell-eaws-lh1"},
                clear=True,
            ),
            raises_system_exit(),
        ):
            validate_run_preflight(args)

    @staticmethod
    def test_non_local_cell_requires_workload_registry() -> None:
        args = RunArgs("ray", "src/ray", "pub", "man", "team-ex", "k")
        with (
            patch.dict(
                os.environ,
                {"WORKLOAD_RUN_ID": "r1", "WORKLOAD_TARGET_CELL": "cell-prod-01"},
                clear=True,
            ),
            raises_system_exit(),
        ):
            validate_run_preflight(args)


class TestManifestMutation(unittest.TestCase):
    """Test AST-level manifest mutation and placeholder replacement."""

    @staticmethod
    def test_single_image_and_placeholders_substitution() -> None:
        manifest = {
            "metadata": {
                "generateName": "template-prefix-",
                "labels": {"team": "examples"},
            },
            "spec": {
                "image": "registry.invalid/workloads/ray-train:latest",
                "run": "__WORKLOAD_RUN_ID__",
                "cell": "__WORKLOAD_CELL_VIRTUAL__",
            },
        }
        deployment_refs = {
            "": "deploy.registry/ray_train@sha256:1111111111111111111111111111111111111111111111111111111111111111"
        }
        config = MutationConfig(
            workload="ray-train",
            run_name="ray-train-alice-run01",
            run_id="run01",
            launcher="alice",
            team_namespace="team-examples",
            virtual_cell="eaws-lh1",
            deployment_refs=deployment_refs,
        )
        mutated = mutate_manifest(manifest, config)
        metadata = mutated.get("metadata")
        assert isinstance(metadata, dict)
        assert isinstance(metadata, dict)
        assert "generateName" not in metadata
        assert metadata["name"] == "ray-train-alice-run01"
        assert metadata["namespace"] == "team-examples"
        labels = metadata.get("labels")
        assert isinstance(labels, dict)
        assert labels["pipeline"] == "ray-train"
        assert labels["run-id"] == "run01"
        assert labels["user"] == "alice"
        assert labels["team"] == "examples"

        spec = mutated.get("spec")
        assert isinstance(spec, dict)
        assert isinstance(spec, dict)
        assert spec["image"] == deployment_refs[""]
        assert spec["run"] == "ray-train-alice-run01"
        assert spec["cell"] == "eaws-lh1"

    @staticmethod
    def test_multi_role_image_substitution() -> None:
        manifest = {
            "spec": {
                "head": "registry.invalid/workloads/ray-train/head:latest",
                "worker": "registry.invalid/workloads/ray-train-worker@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            },
        }
        deployment_refs = {
            "head": "deploy.registry/ray_train@sha256:head1234",
            "worker": "deploy.registry/ray_train@sha256:work1234",
        }
        config = MutationConfig(
            workload="ray-train",
            run_name="ray-train-user-r1",
            run_id="r1",
            launcher="user",
            team_namespace="team-examples",
            virtual_cell="eaws-lh1",
            deployment_refs=deployment_refs,
        )
        mutated = mutate_manifest(manifest, config)
        spec = mutated.get("spec")
        assert isinstance(spec, dict)
        assert spec["head"] == "deploy.registry/ray_train@sha256:head1234"
        assert spec["worker"] == "deploy.registry/ray_train@sha256:work1234"

    @staticmethod
    def test_extra_tag_pattern() -> None:
        assert EXTRA_TAG_PATTERN.match("latest") is not None
        assert EXTRA_TAG_PATTERN.match("_tag-1.0") is not None
        assert EXTRA_TAG_PATTERN.match("-invalid") is None
        assert EXTRA_TAG_PATTERN.match(".invalid") is None
        assert EXTRA_TAG_PATTERN.match("invalid:tag") is None

    @staticmethod
    def test_parse_extra_tags_valid_inputs() -> None:
        cases = [
            ("main,latest", ["main", "latest"]),
            ("main latest", ["main", "latest"]),
            ("main, latest   v1.0", ["main", "latest", "v1.0"]),
            ("  tag-a ,  tag_b   ", ["tag-a", "tag_b"]),
            ("main,latest,main", ["main", "latest"]),
            ("a" * 128, ["a" * 128]),
            ("_lead-underscore.1", ["_lead-underscore.1"]),
            ("", []),
            ("   ", []),
            (",", []),
            (", ,", []),
        ]
        for raw, expected in cases:
            assert parse_extra_tags(raw) == expected
            with patch.dict(os.environ, {"WORKLOAD_EXTRA_TAGS": raw}):
                assert parse_extra_tags() == expected

    @staticmethod
    def test_parse_extra_tags_invalid_inputs_exit() -> None:
        invalid_tags = [
            "-starts-with-dash",
            ".starts-with-dot",
            "tag:with-colon",
            "tag/with-slash",
            "tag@with-digest",
            "a" * 129,
            "valid, -invalid",
            "valid, bad:tag",
        ]
        for invalid in invalid_tags:
            with raises_system_exit(1):
                parse_extra_tags(invalid)
            with patch.dict(os.environ, {"WORKLOAD_EXTRA_TAGS": invalid}), raises_system_exit(1):
                parse_extra_tags()


class TestRayDashboard(unittest.TestCase):
    """Test validation of Ray dashboard URL contracts."""

    @staticmethod
    def test_valid_dashboard_url() -> None:
        submitted = {
            "apiVersion": "ray.io/v1",
            "kind": "RayJob",
            "metadata": {
                "name": "job-1",
                "namespace": "team-nlp",
                "labels": {"team": "nlp"},
                "annotations": {
                    "dashboard-url": "https://ray.cell-eaws-lh1.c.unit.test/teams/nlp/namespaces/team-nlp/jobs/job-1/",
                },
            },
        }
        # Should not raise
        check_ray_dashboard(
            submitted, run_name="job-1", target_cell="cell-eaws-lh1", team_namespace="team-nlp"
        )

    @staticmethod
    def test_malformed_dashboard_url_raises() -> None:
        submitted = {
            "apiVersion": "ray.io/v1",
            "kind": "RayJob",
            "metadata": {
                "name": "job-1",
                "namespace": "team-nlp",
                "labels": {"team": "nlp"},
                "annotations": {
                    "dashboard-url": "https://attacker.com/malicious",
                },
            },
        }
        with raises_system_exit():
            check_ray_dashboard(
                submitted,
                run_name="job-1",
                target_cell="cell-eaws-lh1",
                team_namespace="team-nlp",
            )


class TestLocalPublication(unittest.TestCase):
    """Protect immutable stream tags and verified registry reads during publication."""

    @override
    def setUp(self) -> None:
        self.manifests: dict[str, str] = {}
        self.read_status = 200
        test = self

        class Registry(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                reference = self.path.rsplit("/", 1)[-1]
                status = test.read_status if reference in test.manifests else 404
                self.send_response(status)
                if reference in test.manifests:
                    self.send_header("Docker-Content-Digest", test.manifests[reference])
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Registry)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = patch.dict(os.environ, {"WORKLOAD_REGISTRY_INSECURE": "true"})
        self.environment.start()
        self.delivery = ImageDelivery(
            pusher="pusher",
            repository=f"127.0.0.1:{self.server.server_port}/000000000000/us-west-2/src/examples/ray_train",
            digest="sha256:" + "a" * 64,
            tag="dev-20260915T120000Z_0123456789ab",
            publication_mode="stream",
            crane="crane",
        )

    @override
    def tearDown(self) -> None:
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_existing_stream_tag_is_reused_only_for_the_same_digest(self) -> None:
        for digest in (self.delivery.digest, "sha256:" + "b" * 64):
            with self.subTest(digest=digest), patch("subprocess.run") as run:
                self.manifests[self.delivery.tag] = digest
                if digest == self.delivery.digest:
                    deliver_to_local_registry(self.delivery)
                else:
                    with raises_system_exit(1):
                        deliver_to_local_registry(self.delivery)
                run.assert_not_called()

    def test_missing_stream_tag_is_published_once_with_or_without_cached_image(self) -> None:
        for cached in (True, False):
            with self.subTest(cached=cached):
                self.manifests = {self.delivery.digest: self.delivery.digest} if cached else {}

                def publish(
                    command: list[str], **_kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    self.manifests[self.delivery.tag] = self.delivery.digest
                    self.manifests[self.delivery.digest] = self.delivery.digest
                    return subprocess.CompletedProcess(command, 0, "", "")

                with patch("subprocess.run", side_effect=publish) as run:
                    deliver_to_local_registry(self.delivery)
                    deliver_to_local_registry(self.delivery)
                expected = (
                    [
                        "crane",
                        "tag",
                        "--insecure",
                        f"{self.delivery.repository}@{self.delivery.digest}",
                        self.delivery.tag,
                    ]
                    if cached
                    else [
                        "pusher",
                        "--insecure",
                        "--repository",
                        self.delivery.repository,
                        "--tag",
                        self.delivery.tag,
                    ]
                )
                assert [call.args[0] for call in run.call_args_list] == [expected]

    def test_registry_connection_failure_never_attempts_publication(self) -> None:
        with (
            patch(
                "urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")
            ),
            patch("subprocess.run") as run,
            raises_system_exit(1),
        ):
            deliver_to_local_registry(self.delivery)
        run.assert_not_called()

    def test_registry_read_failure_never_attempts_publication(self) -> None:
        for status, digest in [
            (401, self.delivery.digest),
            (403, self.delivery.digest),
            (500, self.delivery.digest),
            (200, ""),
            (200, "invalid"),
        ]:
            with self.subTest(status=status, digest=digest):
                self.read_status = status
                self.manifests[self.delivery.tag] = digest
                with patch("subprocess.run") as run, raises_system_exit(1):
                    deliver_to_local_registry(self.delivery)
                run.assert_not_called()

    def test_extra_tags_published_in_cache_hit_and_push_paths(self) -> None:
        target_ref = f"{self.delivery.repository}@{self.delivery.digest}"
        for cached in (True, False):
            with self.subTest(cached=cached):
                self.manifests = {self.delivery.digest: self.delivery.digest} if cached else {}

                def publish(
                    command: list[str], **_kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    self.manifests[self.delivery.tag] = self.delivery.digest
                    self.manifests[self.delivery.digest] = self.delivery.digest
                    return subprocess.CompletedProcess(command, 0, "", "")

                env = {"WORKLOAD_EXTRA_TAGS": "main,latest"}
                with (
                    patch.dict(os.environ, env),
                    patch("subprocess.run", side_effect=publish) as run,
                ):
                    deliver_to_local_registry(self.delivery)

                expected = (
                    [
                        [
                            "crane",
                            "tag",
                            "--insecure",
                            target_ref,
                            self.delivery.tag,
                        ],
                        [
                            "crane",
                            "tag",
                            "--insecure",
                            target_ref,
                            "main",
                        ],
                        [
                            "crane",
                            "tag",
                            "--insecure",
                            target_ref,
                            "latest",
                        ],
                    ]
                    if cached
                    else [
                        [
                            "pusher",
                            "--insecure",
                            "--repository",
                            self.delivery.repository,
                            "--tag",
                            self.delivery.tag,
                        ],
                        [
                            "crane",
                            "tag",
                            "--insecure",
                            target_ref,
                            "main",
                        ],
                        [
                            "crane",
                            "tag",
                            "--insecure",
                            target_ref,
                            "latest",
                        ],
                    ]
                )
                assert [call.args[0] for call in run.call_args_list] == expected

    def test_invalid_extra_tag_rejects_before_local_delivery(self) -> None:
        with (
            patch.dict(os.environ, {"WORKLOAD_EXTRA_TAGS": "invalid:tag"}),
            patch("subprocess.run") as run,
            raises_system_exit(1),
        ):
            deliver_to_local_registry(self.delivery)
        run.assert_not_called()


class TestEcrPublication(unittest.TestCase):
    """Test AWS ECR image delivery and extra tags behavior."""

    @override
    def setUp(self) -> None:
        self.delivery = ImageDelivery(
            pusher="pusher",
            repository="123456789012.dkr.ecr.us-east-1.amazonaws.com/src/examples/ray_train",
            digest="sha256:" + "a" * 64,
            tag="dev-20260915T120000Z_0123456789ab",
            publication_mode="stream",
            crane="crane",
        )

    def test_extra_tags_ecr_cache_hit_and_push_paths(self) -> None:
        target_ref = f"{self.delivery.repository}@{self.delivery.digest}"
        ref = f"{self.delivery.repository}:{self.delivery.tag}"

        for cached in (True, False):
            with self.subTest(cached=cached):

                def fake_run(
                    cmd: list[str], *, is_cached: bool = cached, **_kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    if cmd[:2] == ["crane", "digest"] and cmd[2] == target_ref:
                        if is_cached:
                            return subprocess.CompletedProcess(
                                cmd, 0, f"{self.delivery.digest}\n", ""
                            )
                        return subprocess.CompletedProcess(cmd, 1, "", "not found")
                    if cmd[:2] == ["crane", "digest"] and cmd[2] == ref:
                        return subprocess.CompletedProcess(cmd, 0, f"{self.delivery.digest}\n", "")
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                env = {
                    "WORKLOAD_EXTRA_TAGS": "main,latest",
                }
                with (
                    patch.dict(os.environ, env),
                    patch(f"{deliver_to_ecr.__module__}.validate_ecr_identity"),
                    patch("subprocess.run", side_effect=fake_run) as run,
                ):
                    deliver_to_ecr(self.delivery)

                tag_calls = [
                    call.args[0]
                    for call in run.call_args_list
                    if call.args[0][:2] == ["crane", "tag"]
                ]
                expected_tag_calls = (
                    [
                        ["crane", "tag", target_ref, self.delivery.tag],
                        ["crane", "tag", target_ref, "main"],
                        ["crane", "tag", target_ref, "latest"],
                    ]
                    if cached
                    else [
                        ["crane", "tag", target_ref, "main"],
                        ["crane", "tag", target_ref, "latest"],
                    ]
                )
                assert tag_calls == expected_tag_calls

                pusher_calls = [
                    call.args[0] for call in run.call_args_list if call.args[0][0] == "pusher"
                ]
                if cached:
                    assert not pusher_calls
                else:
                    assert pusher_calls == [
                        [
                            "pusher",
                            "--repository",
                            self.delivery.repository,
                            "--tag",
                            self.delivery.tag,
                        ]
                    ]

    def test_cache_hit_skips_stream_tag_when_newest_stream_tag_names_digest(self) -> None:
        target_ref = f"{self.delivery.repository}@{self.delivery.digest}"
        other_digest = "sha256:" + "b" * 64
        cases = {
            "newest tag names digest": (
                "dev-20260914T120000Z_aaaaaaaaaaaa",
                self.delivery.digest,
                False,
            ),
            "newest tag names another digest": (
                "dev-20260914T120000Z_aaaaaaaaaaaa",
                other_digest,
                True,
            ),
            "no stream tag yet": (None, None, True),
        }
        for name, (newest, newest_digest, expect_tag) in cases.items():
            with self.subTest(name):
                listing = "\n".join(
                    filter(
                        None,
                        [
                            "latest",
                            "dev-20260901T120000Z_bbbbbbbbbbbb",
                            "ci-20260930T120000Z_cccccccccccc",
                            "dev-20260930T120000Z_dddddddddddd-worker",
                            newest,
                        ],
                    )
                )

                def fake_run(
                    cmd: list[str],
                    *,
                    tags: str = listing,
                    tagged: str | None = newest,
                    tagged_digest: str | None = newest_digest,
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    if cmd[:2] == ["crane", "digest"] and cmd[2] == target_ref:
                        return subprocess.CompletedProcess(cmd, 0, f"{self.delivery.digest}\n", "")
                    if cmd[:2] == ["crane", "ls"]:
                        return subprocess.CompletedProcess(cmd, 0, f"{tags}\n", "")
                    if cmd[:2] == ["crane", "digest"] and tagged and cmd[2].endswith(f":{tagged}"):
                        return subprocess.CompletedProcess(cmd, 0, f"{tagged_digest}\n", "")
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                with (
                    patch.dict(os.environ, {}, clear=False),
                    patch(f"{deliver_to_ecr.__module__}.validate_ecr_identity"),
                    patch("subprocess.run", side_effect=fake_run) as run,
                ):
                    os.environ.pop("WORKLOAD_EXTRA_TAGS", None)
                    deliver_to_ecr(self.delivery)

                tag_calls = [
                    call.args[0]
                    for call in run.call_args_list
                    if call.args[0][:2] == ["crane", "tag"]
                ]
                assert tag_calls == (
                    [["crane", "tag", target_ref, self.delivery.tag]] if expect_tag else []
                )

    def test_invalid_extra_tag_rejects_before_ecr_delivery(self) -> None:
        with (
            patch.dict(os.environ, {"WORKLOAD_EXTRA_TAGS": "bad:tag"}),
            patch(f"{deliver_to_ecr.__module__}.validate_ecr_identity"),
            patch("subprocess.run") as run,
            raises_system_exit(1),
        ):
            deliver_to_ecr(self.delivery)
        run.assert_not_called()


class TestPublishAndRunExecution(unittest.TestCase):
    """Test full execution paths for publish and run."""

    @staticmethod
    def test_parse_publish_single_image() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            idx_file = Path(tmpdir) / "index.json"
            idx_file.write_text(
                json.dumps({
                    "manifests": [
                        {
                            "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        }
                    ]
                }),
                encoding="utf-8",
            )
            roles, pushers, digests = parse_publish_images("target_pusher", str(idx_file), tmpdir)
            assert roles == [""]
            assert pushers == ["target_pusher"]
            assert digests == [
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ]

    @staticmethod
    def test_parse_publish_multi_role() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            idx1 = Path(tmpdir) / "idx1.json"
            idx2 = Path(tmpdir) / "idx2.json"
            d1 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            d2 = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            idx1.write_text(json.dumps({"manifests": [{"digest": d1}]}), encoding="utf-8")
            idx2.write_text(json.dumps({"manifests": [{"digest": d2}]}), encoding="utf-8")

            pusher_arg = f"head=push_head={idx1},worker=push_worker={idx2}"
            roles, pushers, digests = parse_publish_images(pusher_arg, "-", tmpdir)
            assert roles == ["head", "worker"]
            assert pushers == ["push_head", "push_worker"]
            assert digests == [d1, d2]

    @staticmethod
    def test_determine_publish_repository_floci() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / ".tmp/state"
            state_dir.mkdir(parents=True)
            contract = state_dir / "origin-registry.json"
            contract.write_text(
                json.dumps({
                    "version": 1,
                    "host": {
                        "repositories": {
                            "src/examples/ray_train": "localhost:15100/000000000000/us-east-1/src/examples/ray_train"
                        }
                    },
                }),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                repo, is_local = determine_publish_repository(tmpdir, "src/examples/ray_train")
                assert repo == "localhost:15100/000000000000/us-east-1/src/examples/ray_train"
                assert is_local

    @staticmethod
    def test_determine_publish_repository_accepts_local_seed_root() -> None:
        environment = {
            "WORKLOAD_REGISTRY": "127.0.0.1:15100/000000000000/us-east-1",
            "WORKLOAD_REGISTRY_INSECURE": "true",
        }
        with patch.dict(os.environ, environment, clear=True):
            repository, is_local = determine_publish_repository(
                "/missing-workspace", "src/examples/ray_train"
            )

        assert repository == "127.0.0.1:15100/000000000000/us-east-1/src/examples/ray_train"
        assert is_local

    def test_determine_publish_repository_accepts_floci_secure_root(self) -> None:
        for reg in (
            "127.0.0.1:4566/000000000000/us-east-1",
            "localhost:4566/000000000000/us-west-2",
            "000000000000.dkr.ecr.us-east-1.localhost.floci.io:4566",
            "127.0.0.1:15100/000000000000/us-east-1",
        ):
            with (
                self.subTest(registry=reg),
                patch.dict(
                    os.environ,
                    {"WORKLOAD_REGISTRY": reg, "WORKLOAD_REGISTRY_INSECURE": "false"},
                    clear=True,
                ),
            ):
                repository, is_local = determine_publish_repository(
                    "/missing-workspace", "src/examples/ray_train"
                )
                assert repository == f"{reg}/src/examples/ray_train"
                assert is_local

    def test_determine_publish_repository_rejects_unregistered_insecure_root(self) -> None:
        registries = (
            "172.19.255.22:15100/000000000000/us-east-1",
            "127.0.0.1:15100",
            "127.0.0.1:15099/000000000000/us-east-1",
        )
        for registry in registries:
            with (
                self.subTest(registry=registry),
                patch.dict(
                    os.environ,
                    {"WORKLOAD_REGISTRY": registry, "WORKLOAD_REGISTRY_INSECURE": "true"},
                    clear=True,
                ),
                raises_system_exit(),
            ):
                determine_publish_repository("/missing-workspace", "src/examples/ray_train")

    @staticmethod
    def test_determine_stream_tag_explicit() -> None:
        with patch.dict(os.environ, {"WORKLOAD_STREAM_TAG": "ci-20260901T120000Z_0123456789ab"}):
            assert determine_stream_tag("/fake/workspace") == "ci-20260901T120000Z_0123456789ab"
        with patch.dict(os.environ, {"WORKLOAD_STREAM_TAG": "20260901T120000Z_0123456789ab"}):
            assert determine_stream_tag("/fake/workspace") == "20260901T120000Z_0123456789ab"

    @staticmethod
    @patch("subprocess.check_output")
    def test_determine_stream_tag_auto_ci_and_dev(mock_output: MagicMock) -> None:
        mock_output.return_value = "20260901T120000Z_0123456789ab\n"
        with patch.dict(os.environ, {"CI": "true"}, clear=True):
            assert determine_stream_tag("/fake/workspace") == "ci-20260901T120000Z_0123456789ab"
        with patch.dict(os.environ, {}, clear=True):
            assert determine_stream_tag("/fake/workspace") == "dev-20260901T120000Z_0123456789ab"
        with patch.dict(
            os.environ, {"WORKLOAD_TAG_PREFIX": "ci", "WORKLOAD_STREAM_TAG": ""}, clear=True
        ):
            assert determine_stream_tag("/fake/workspace") == "ci-20260901T120000Z_0123456789ab"

    @staticmethod
    def test_stream_tag_script_executes_in_workspace_instead_of_launcher_directory() -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            script = workspace / "src/bazel/rules/oci/stream_tag.sh"
            script.parent.mkdir(parents=True)
            script.write_text(
                'test "$(pwd -P)" = "$EXPECTED_WORKSPACE" || exit 1\n'
                'printf "%s\\n" 20260901T120000Z_0123456789ab\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"EXPECTED_WORKSPACE": str(workspace)}, clear=True):
                assert determine_stream_tag(str(workspace)) == "dev-20260901T120000Z_0123456789ab"

    @staticmethod
    def test_determine_stream_tag_invalid() -> None:
        with (
            patch.dict(os.environ, {"WORKLOAD_STREAM_TAG": "invalid-tag"}),
            raises_system_exit(),
        ):
            determine_stream_tag("/fake/workspace")

    @staticmethod
    def test_build_ecr_environment_isolation() -> None:
        tainted_env = {
            "AWS_ACCESS_KEY_ID": "leaked_key",
            "AWS_SECRET_ACCESS_KEY": "leaked_secret",
            "AWS_PROFILE": "admin",
            "HOME": "/home/user",
        }
        with patch.dict(os.environ, tainted_env, clear=True):
            clean_env = build_ecr_environment("/tmp/docker-cfg")
            assert "AWS_ACCESS_KEY_ID" not in clean_env
            assert "AWS_SECRET_ACCESS_KEY" not in clean_env
            assert "AWS_PROFILE" not in clean_env
            assert clean_env["DOCKER_CONFIG"] == "/tmp/docker-cfg"
            assert clean_env["AWS_CONFIG_FILE"] == "/tmp/docker-cfg/aws-config"
            assert clean_env["AWS_SHARED_CREDENTIALS_FILE"] == "/tmp/docker-cfg/aws-credentials"
            assert clean_env["AWS_EC2_METADATA_DISABLED"] == "true"
            assert clean_env["AWS_ECR_DISABLE_CACHE"] == "true"
            assert clean_env["AWS_SDK_LOAD_CONFIG"] == "false"

    @staticmethod
    def test_validate_ecr_identity_checks() -> None:
        # Missing helper
        with patch("shutil.which", return_value=None), raises_system_exit():
            validate_ecr_identity()

        # Both web and eks identity -> conflicting
        with patch("shutil.which", return_value="/bin/ecr-login"):
            conflicting_env = {
                "AWS_ROLE_ARN": "arn:aws:iam::123:role/foo",
                "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/token",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
                "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": "/tmp/token2",
            }
            with patch.dict(os.environ, conflicting_env, clear=True), raises_system_exit():
                validate_ecr_identity()

    @staticmethod
    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_execute_publish_local_stream(mock_run: MagicMock, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value.__enter__.return_value.headers = {
            "Docker-Content-Digest": "sha256:" + "a" * 64,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            idx_file = Path(tmpdir) / "index.json"
            idx_file.write_text(
                json.dumps({
                    "manifests": [
                        {
                            "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        }
                    ]
                }),
                encoding="utf-8",
            )
            state_dir = Path(tmpdir) / ".tmp/state"
            state_dir.mkdir(parents=True)
            (state_dir / "origin-registry.json").write_text(
                json.dumps({
                    "version": 1,
                    "host": {
                        "repositories": {
                            "src/examples/ray_train": "localhost:15100/000000000000/us-east-1/src/examples/ray_train"
                        }
                    },
                }),
                encoding="utf-8",
            )
            pusher_bin = Path(tmpdir) / "pusher"
            pusher_bin.touch(mode=0o755)
            crane_bin = Path(tmpdir) / "crane"
            crane_bin.touch(mode=0o755)

            env = {
                "BUILD_WORKSPACE_DIRECTORY": tmpdir,
                "WORKLOAD_STREAM_TAG": "20260901T120000Z_0123456789ab",
            }
            args = PublishArgs(
                workload="ray-train",
                repository_path="src/examples/ray_train",
                pusher=str(pusher_bin),
                index=str(idx_file),
                crane=str(crane_bin),
                publication_mode="stream",
            )
            with patch.dict(os.environ, env, clear=True):
                execute_publish(args)
                assert mock_urlopen.called
                mock_run.assert_not_called()

    @staticmethod
    @patch("subprocess.run")
    def test_execute_publish_ecr_cache_hit_retags(mock_run: MagicMock) -> None:
        digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        def fake_run(cmd: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            cmd_str = " ".join(cmd)
            if "crane digest" in cmd_str:
                return MagicMock(returncode=0, stdout=f"{digest}\n")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = fake_run
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch(
                "src.bazel.rules.oci.workload_cli.validate_ecr_identity"
                if "src.bazel" in sys.modules
                else "workload_cli.validate_ecr_identity",
                return_value=None,
            ),
        ):
            idx_file = Path(tmpdir) / "index.json"
            idx_file.write_text(json.dumps({"manifests": [{"digest": digest}]}), encoding="utf-8")
            pusher_bin = Path(tmpdir) / "pusher"
            pusher_bin.touch(mode=0o755)
            crane_bin = Path(tmpdir) / "crane"
            crane_bin.touch(mode=0o755)

            env = {
                "BUILD_WORKSPACE_DIRECTORY": tmpdir,
                "WORKLOAD_REGISTRY": "123456789012.dkr.ecr.us-east-1.amazonaws.com",
                "WORKLOAD_STREAM_TAG": "20260901T120000Z_0123456789ab",
            }
            args = PublishArgs(
                workload="ray-train",
                repository_path="src/examples/ray_train",
                pusher=str(pusher_bin),
                index=str(idx_file),
                crane=str(crane_bin),
                publication_mode="stream",
            )
            with patch.dict(os.environ, env, clear=True):
                execute_publish(args)

            # Check that pusher was NEVER invoked
            for call in mock_run.call_args_list:
                cmd = call[0][0]
                assert str(pusher_bin) not in cmd

            # Check that crane tag WAS invoked to retag
            tag_calls = [call for call in mock_run.call_args_list if "tag" in call[0][0]]
            assert len(tag_calls) == 1
            assert "20260901T120000Z_0123456789ab" in tag_calls[0][0][0]

    @staticmethod
    @patch("subprocess.run")
    def test_execute_publish_cache_hit_tags_extra_tags(mock_run: MagicMock) -> None:
        digest = "sha256:" + "a" * 64

        def fake_run(cmd: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            if "digest" in cmd:
                return MagicMock(returncode=0, stdout=f"{digest}\n")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = fake_run

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch(
                "src.bazel.rules.oci.workload_cli.validate_ecr_identity"
                if "src.bazel" in sys.modules
                else "workload_cli.validate_ecr_identity",
                return_value=None,
            ),
        ):
            idx_file = Path(tmpdir) / "index.json"
            idx_file.write_text(json.dumps({"manifests": [{"digest": digest}]}), encoding="utf-8")
            pusher_bin = Path(tmpdir) / "pusher"
            pusher_bin.touch(mode=0o755)
            crane_bin = Path(tmpdir) / "crane"
            crane_bin.touch(mode=0o755)

            env = {
                "BUILD_WORKSPACE_DIRECTORY": tmpdir,
                "WORKLOAD_REGISTRY": "123456789012.dkr.ecr.us-east-1.amazonaws.com",
                "WORKLOAD_STREAM_TAG": "20260901T120000Z_0123456789ab",
                "WORKLOAD_EXTRA_TAGS": "main,latest",
            }
            args = PublishArgs(
                workload="ray-train",
                repository_path="src/examples/ray_train",
                pusher=str(pusher_bin),
                index=str(idx_file),
                crane=str(crane_bin),
                publication_mode="stream",
            )
            with patch.dict(os.environ, env, clear=True):
                execute_publish(args)

            # Check that pusher was NEVER invoked
            for call in mock_run.call_args_list:
                cmd = call[0][0]
                assert str(pusher_bin) not in cmd

            # Check that crane tag was invoked for stream tag and extra tags
            tag_calls = [call for call in mock_run.call_args_list if "tag" in call[0][0]]
            assert len(tag_calls) == 3
            tagged = [call[0][0][-1] for call in tag_calls]
            assert tagged == ["20260901T120000Z_0123456789ab", "main", "latest"]

    @staticmethod
    @patch("subprocess.run")
    def test_execute_run_success(mock_run: MagicMock) -> None:
        def fake_run(cmd: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            cmd_str = " ".join(cmd)
            stdout = ""
            if "config current-context" in cmd_str:
                stdout = "cell-eaws-lh1\n"
            elif "auth can-i" in cmd_str:
                stdout = "yes\n"
            elif "get configmap workload-repositories" in cmd_str:
                cm = {
                    "data": {
                        "repositories.json": json.dumps({
                            "version": 1,
                            "repositories": {"src/examples/ray_data": "localhost:15100/repo"},
                        })
                    }
                }
                stdout = json.dumps(cm)
            elif "create --dry-run=client" in cmd_str:
                m = {
                    "apiVersion": "ray.io/v1",
                    "kind": "RayJob",
                    "metadata": {"name": "old"},
                    "spec": {"image": "registry.invalid/workloads/ray-data"},
                }
                stdout = json.dumps(m)
            elif "create --filename=-" in cmd_str:
                res = {
                    "apiVersion": "ray.io/v1",
                    "kind": "RayJob",
                    "metadata": {
                        "name": "ray-data-user-run01",
                        "namespace": "team-examples",
                        "labels": {"team": "examples"},
                        "annotations": {
                            "dashboard-url": "https://ray.cell-eaws-lh1.c.unit.test/teams/examples/namespaces/team-examples/jobs/ray-data-user-run01/",
                        },
                    },
                }
                stdout = json.dumps(res)
            elif "publisher" in cmd_str:
                stdout = json.dumps({"": "origin/repo@sha256:abcd"})
            return MagicMock(returncode=0, stdout=stdout)

        mock_run.side_effect = fake_run

        with tempfile.TemporaryDirectory() as tmpdir:
            kubectl = Path(tmpdir) / "kubectl"
            kubectl.touch(mode=0o755)
            publisher = Path(tmpdir) / "publisher"
            publisher.touch(mode=0o755)
            manifest = Path(tmpdir) / "manifest.yaml"
            manifest.write_text('{"kind": "RayJob"}', encoding="utf-8")
            kubeconfig_dir = Path(tmpdir) / ".tmp/kubeconfigs"
            kubeconfig_dir.mkdir(parents=True)
            (kubeconfig_dir / "cell-eaws-lh1.yaml").touch()

            env = {
                "BUILD_WORKSPACE_DIRECTORY": tmpdir,
                "WORKLOAD_RUN_ID": "run01",
                "WORKLOAD_LAUNCHER": "user",
                "WORKLOAD_TARGET_CELL": "cell-eaws-lh1",
            }
            args = RunArgs(
                workload="ray-data",
                repository_path="src/examples/ray_data",
                publisher=str(publisher),
                manifest=str(manifest),
                team_namespace="team-examples",
                kubectl=str(kubectl),
            )
            with patch.dict(os.environ, env, clear=True):
                execute_run(args)
                assert mock_run.called

    def test_execute_run_stops_before_create_when_rbac_check_fails(self) -> None:
        kubectl_error = "error: You must be logged in to the server (Unauthorized)\n"
        cases = [
            ("denied", subprocess.CalledProcessError(1, "kubectl", output="no\n", stderr="")),
            (
                "kubectl failure",
                subprocess.CalledProcessError(1, "kubectl", output="", stderr=kubectl_error),
            ),
        ]
        for name, can_i_error in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:

                def fake_run(
                    cmd: list[str], *_args: object, err: Exception = can_i_error, **_kwargs: object
                ) -> MagicMock:
                    cmd_str = " ".join(cmd)
                    if "config current-context" in cmd_str:
                        return MagicMock(returncode=0, stdout="cell-eaws-lh1\n")
                    if "auth can-i" in cmd_str:
                        raise err
                    return MagicMock(returncode=0, stdout="")

                kubectl = Path(tmpdir) / "kubectl"
                kubectl.touch(mode=0o755)
                publisher = Path(tmpdir) / "publisher"
                publisher.touch(mode=0o755)
                manifest = Path(tmpdir) / "manifest.yaml"
                manifest.write_text('{"kind": "RayJob"}', encoding="utf-8")
                kubeconfig_dir = Path(tmpdir) / ".tmp/kubeconfigs"
                kubeconfig_dir.mkdir(parents=True)
                (kubeconfig_dir / "cell-eaws-lh1.yaml").touch()
                env = {
                    "BUILD_WORKSPACE_DIRECTORY": tmpdir,
                    "WORKLOAD_RUN_ID": "run01",
                    "WORKLOAD_LAUNCHER": "user",
                    "WORKLOAD_TARGET_CELL": "cell-eaws-lh1",
                }
                args = RunArgs(
                    workload="ray-data",
                    repository_path="src/examples/ray_data",
                    publisher=str(publisher),
                    manifest=str(manifest),
                    team_namespace="team-examples",
                    kubectl=str(kubectl),
                )
                with (
                    patch("subprocess.run", side_effect=fake_run) as mock_run,
                    patch("sys.stderr") as stderr,
                    patch.dict(os.environ, env, clear=True),
                    raises_system_exit(1),
                ):
                    execute_run(args)

                commands = [" ".join(call[0][0]) for call in mock_run.call_args_list]
                assert not any("create --filename=-" in cmd for cmd in commands)
                written = "".join(call[0][0] for call in stderr.write.call_args_list)
                assert (kubectl_error in written) == (can_i_error.stderr == kubectl_error)

    @staticmethod
    def test_main_subcommand_dispatch() -> None:
        with patch("sys.argv", ["workload_cli.py"]), raises_system_exit():
            main()


if __name__ == "__main__":
    unittest.main()
