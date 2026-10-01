"""Unit tests defending diff-aware CLI argument parsing, target normalization, and execution dispatch."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

if TYPE_CHECKING:
    from collections.abc import Sequence

try:
    from src.bazel.tools.diff.bazel_diff import BazelDiffResult
    from src.bazel.tools.diff.cli import (
        ParsedArgs,
        dispatch,
        main,
        parse_cli_args,
        run_check,
        run_fix,
        run_publish,
        run_test,
    )
except ImportError:  # pragma: no cover
    try:
        from .bazel_diff import BazelDiffResult  # type: ignore[no-redef]
        from .cli import (  # type: ignore[no-redef]
            ParsedArgs,
            dispatch,
            main,
            parse_cli_args,
            run_check,
            run_fix,
            run_publish,
            run_test,
        )
    except ImportError:  # pragma: no cover
        from bazel_diff import BazelDiffResult  # type: ignore[no-redef]
        from cli import (  # type: ignore[no-redef]
            ParsedArgs,
            dispatch,
            main,
            parse_cli_args,
            run_check,
            run_fix,
            run_publish,
            run_test,
        )


class CliArgParsingTest(unittest.TestCase):
    """Defends command-line parsing separating subcommands, targets, and Bazel flags."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name).resolve()
        (self.repo_root / "BUILD.bazel").touch()
        pkg = self.repo_root / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "BUILD.bazel").touch()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_arguments_defaults_to_test(self) -> None:
        parsed = parse_cli_args([], repo_root=self.repo_root)
        self.assertEqual(parsed.subcommand, "test")
        self.assertFalse(parsed.run_all)
        self.assertEqual(parsed.explicit_targets, [])
        self.assertEqual(parsed.bazel_flags, [])

    def test_parses_subcommands_correctly(self) -> None:
        for sub in ("test", "check", "fix", "publish"):
            parsed = parse_cli_args([sub], repo_root=self.repo_root)
            self.assertEqual(parsed.subcommand, sub)
            self.assertFalse(parsed.run_all)
            self.assertEqual(parsed.explicit_targets, [])

    def test_parse_cli_args_publish(self) -> None:
        parsed = parse_cli_args(["publish", "//src/infra:publish"], repo_root=self.repo_root)
        self.assertEqual(parsed.subcommand, "publish")
        self.assertFalse(parsed.run_all)
        self.assertEqual(parsed.explicit_targets, ["//src/infra:publish"])

        parsed_all = parse_cli_args(["publish", "--all"], repo_root=self.repo_root)
        self.assertEqual(parsed_all.subcommand, "publish")
        self.assertTrue(parsed_all.run_all)

        parsed_short_all = parse_cli_args(["publish", "-a"], repo_root=self.repo_root)
        self.assertEqual(parsed_short_all.subcommand, "publish")
        self.assertTrue(parsed_short_all.run_all)

    def test_parses_all_flag(self) -> None:
        parsed = parse_cli_args(["test", "--all"], repo_root=self.repo_root)
        self.assertTrue(parsed.run_all)
        self.assertEqual(parsed.explicit_targets, [])

        parsed_short = parse_cli_args(["check", "-a"], repo_root=self.repo_root)
        self.assertTrue(parsed_short.run_all)

    def test_ci_push_to_main_implies_all(self) -> None:
        env = {"CI": "true", "GIT_BRANCH": "main", "GIT_PR_NUMBER": "0"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertTrue(parse_cli_args(["test"], repo_root=self.repo_root).run_all)
            self.assertTrue(parse_cli_args([], repo_root=self.repo_root).run_all)
        with mock.patch.dict("os.environ", {**env, "GIT_PR_NUMBER": "7"}, clear=True):
            self.assertFalse(parse_cli_args(["test"], repo_root=self.repo_root).run_all)

    def test_github_push_to_main_implies_all(self) -> None:
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF_NAME": "main",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertTrue(parse_cli_args(["test"], repo_root=self.repo_root).run_all)
        pull_request = {**env, "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF_NAME": "7/merge"}
        with mock.patch.dict("os.environ", pull_request, clear=True):
            self.assertFalse(parse_cli_args(["test"], repo_root=self.repo_root).run_all)

    def test_normalizes_positional_targets(self) -> None:
        parsed = parse_cli_args(["test", "src/pkg"], repo_root=self.repo_root)
        self.assertEqual(parsed.explicit_targets, ["//src/pkg/..."])

        parsed_label = parse_cli_args(["test", "//src/pkg:lib"], repo_root=self.repo_root)
        self.assertEqual(parsed_label.explicit_targets, ["//src/pkg:lib"])

        parsed_dot = parse_cli_args(["check", "."], repo_root=self.repo_root)
        self.assertEqual(parsed_dot.explicit_targets, ["//..."])

    def test_separates_bazel_flags_and_flags_with_values(self) -> None:
        parsed = parse_cli_args(
            [
                "test",
                "-c",
                "dbg",
                "--test_filter",
                "MyTest",
                "--test_arg=--verbose",
                "--keep_going",
                "src/pkg",
            ],
            repo_root=self.repo_root,
        )
        self.assertEqual(parsed.subcommand, "test")
        self.assertEqual(
            parsed.bazel_flags,
            ["-c", "dbg", "--test_filter", "MyTest", "--test_arg=--verbose", "--keep_going"],
        )
        self.assertEqual(parsed.explicit_targets, ["//src/pkg/..."])

    def test_parses_targets_after_dash_dash(self) -> None:
        parsed = parse_cli_args(
            ["test", "--test_arg", "val", "--", "src/pkg", "//src/pkg:test"],
            repo_root=self.repo_root,
        )
        self.assertEqual(parsed.bazel_flags, ["--test_arg", "val"])
        self.assertEqual(parsed.explicit_targets, ["//src/pkg/...", "//src/pkg:test"])


class CliDispatchTest(unittest.TestCase):
    """Defends subcommand execution workflows and command dispatching."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name).resolve()
        (self.repo_root / "BUILD.bazel").touch()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_test_explicit_targets(self, mock_changed: mock.MagicMock) -> None:
        mock_changed.return_value = []
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        parsed = ParsedArgs(
            subcommand="test",
            run_all=False,
            explicit_targets=["//src/foo:foo_test"],
            bazel_flags=["-c", "fastbuild"],
        )
        code = run_test(self.repo_root, parsed, runner=fake_runner)

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("test", commands[0])
        self.assertIn("-c", commands[0])
        self.assertIn("fastbuild", commands[0])
        self.assertIn("//src/foo:foo_test", commands[0])
        mock_changed.assert_not_called()

    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_test_all_flag(self, mock_changed: mock.MagicMock) -> None:
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        parsed = ParsedArgs(
            subcommand="test",
            run_all=True,
            explicit_targets=[],
            bazel_flags=[],
        )
        code = run_test(self.repo_root, parsed, runner=fake_runner)

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("//...", commands[0])
        mock_changed.assert_not_called()

    def test_run_test_output_root_diverges_from_plain_bazel(self) -> None:
        parsed = ParsedArgs(subcommand="test", run_all=True, explicit_targets=[], bazel_flags=[])
        commands: list[list[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(list(cmd))
            return 0

        cases = (
            (
                {"BAZEL_OUTPUT_ROOT": "/state/bazel"},
                ["bazel", "--output_user_root=/state/bazel", "test"],
            ),
            ({}, ["bazel", "test"]),
        )
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.dict("os.environ", env, clear=True):
                commands.clear()
                run_test(self.repo_root, parsed, runner=fake_runner)
                self.assertEqual(commands[0][: len(expected)], expected)

    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_test_no_changed_files(self, mock_changed: mock.MagicMock) -> None:
        mock_changed.return_value = []
        commands: list[Sequence[str]] = []

        out = io.StringIO()
        with redirect_stdout(out):
            code = run_test(
                self.repo_root,
                ParsedArgs(subcommand="test", run_all=False, explicit_targets=[], bazel_flags=[]),
                runner=lambda cmd: commands.append(list(cmd)) or 0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(commands, [])
        self.assertIn("No changed files detected. All targets are up to date.", out.getvalue())

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_test_no_affected_tests(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["docs/readme.md"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=[],
            affected_targets=[],
            affected_tests=[],
            affected_packages=[],
            is_global=False,
        )
        commands: list[Sequence[str]] = []

        out = io.StringIO()
        with redirect_stdout(out):
            code = run_test(
                self.repo_root,
                ParsedArgs(subcommand="test", run_all=False, explicit_targets=[], bazel_flags=[]),
                runner=lambda cmd: commands.append(list(cmd)) or 0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(commands, [])
        self.assertIn("No affected tests detected for changed files.", out.getvalue())

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_test_executes_affected_tests(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["src/pkg/lib.py"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=["//src/pkg:lib"],
            affected_targets=["//src/pkg:lib_test"],
            affected_tests=["//src/pkg:lib_test"],
            affected_packages=["//src/pkg/..."],
            is_global=False,
        )
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_test(
            self.repo_root,
            ParsedArgs(subcommand="test", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("test", commands[0])
        self.assertIn("//src/pkg:lib_test", commands[0])

    def test_run_check_all_flag(self) -> None:
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_check(
            self.repo_root,
            ParsedArgs(subcommand="check", run_all=True, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0][-4:],
            ["run", "//:check", "--", "--all"],
        )

    def test_run_check_explicit_target(self) -> None:
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_check(
            self.repo_root,
            ParsedArgs(
                subcommand="check",
                run_all=False,
                explicit_targets=["//src/pkg/..."],
                bazel_flags=[],
            ),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0][-4:],
            ["run", "//:check", "--", "//src/pkg/..."],
        )

    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_check_no_changed_files(self, mock_changed: mock.MagicMock) -> None:
        mock_changed.return_value = []
        commands: list[Sequence[str]] = []

        out = io.StringIO()
        with redirect_stdout(out):
            code = run_check(
                self.repo_root,
                ParsedArgs(subcommand="check", run_all=False, explicit_targets=[], bazel_flags=[]),
                runner=lambda cmd: commands.append(list(cmd)) or 0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(commands, [])
        self.assertIn("No changed files detected. All checks passed.", out.getvalue())

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_check_affected_packages(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["src/pkg/lib.py"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=["//src/pkg:lib"],
            affected_targets=[],
            affected_tests=[],
            affected_packages=["//src/pkg/..."],
            is_global=False,
        )
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_check(
            self.repo_root,
            ParsedArgs(subcommand="check", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0][-4:],
            ["run", "//:check", "--", "//src/pkg/..."],
        )

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_check_core_file_drift(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["MODULE.bazel"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=["//..."],
            affected_targets=["//..."],
            affected_tests=["//..."],
            affected_packages=["//..."],
            is_global=True,
        )
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_check(
            self.repo_root,
            ParsedArgs(subcommand="check", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            commands[0][-4:],
            ["run", "//:check", "--", "--all"],
        )

    def test_run_fix_all_and_explicit_target(self) -> None:
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        run_fix(
            self.repo_root,
            ParsedArgs(subcommand="fix", run_all=True, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )
        self.assertEqual(commands[0][-4:], ["run", "//:fix", "--", "--all"])

        run_fix(
            self.repo_root,
            ParsedArgs(
                subcommand="fix",
                run_all=False,
                explicit_targets=["//src/pkg/..."],
                bazel_flags=[],
            ),
            runner=fake_runner,
        )
        self.assertEqual(commands[1][-4:], ["run", "//:fix", "--", "//src/pkg/..."])

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_fix_affected(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["src/pkg/lib.py"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=["//src/pkg:lib"],
            affected_targets=[],
            affected_tests=[],
            affected_packages=["//src/pkg/..."],
            is_global=False,
        )
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_fix(
            self.repo_root,
            ParsedArgs(subcommand="fix", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(commands[0][-4:], ["run", "//:fix", "--", "//src/pkg/..."])

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_fix_active_generators_without_affected_packages(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["package.json"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=[],
            affected_targets=[],
            affected_tests=[],
            affected_packages=[],
            is_global=False,
        )
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_fix(
            self.repo_root,
            ParsedArgs(subcommand="fix", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(commands[0][-2:], ["run", "//:fix"])

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_fix_no_active_generators_and_no_affected_packages(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["unknown_file.txt"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=[],
            affected_targets=[],
            affected_tests=[],
            affected_packages=[],
            is_global=False,
        )
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_fix(
            self.repo_root,
            ParsedArgs(subcommand="fix", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(commands, [])

    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_publish_explicit_target(self, mock_changed: mock.MagicMock) -> None:
        mock_changed.return_value = []
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        parsed = ParsedArgs(
            subcommand="publish",
            run_all=False,
            explicit_targets=["//src/infra/docker:publish"],
            bazel_flags=["--stamp"],
        )
        code = run_publish(self.repo_root, parsed, runner=fake_runner)

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("run", commands[0])
        self.assertIn("--stamp", commands[0])
        self.assertIn("//src/infra/docker:publish", commands[0])
        mock_changed.assert_not_called()

        fail_runner = mock.MagicMock(return_value=1)
        code_fail = run_publish(self.repo_root, parsed, runner=fail_runner)
        self.assertEqual(code_fail, 1)

    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_publish_no_changes(self, mock_changed: mock.MagicMock) -> None:
        mock_changed.return_value = []
        commands: list[Sequence[str]] = []

        out = io.StringIO()
        with redirect_stdout(out):
            code = run_publish(
                self.repo_root,
                ParsedArgs(
                    subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]
                ),
                runner=lambda cmd: commands.append(list(cmd)) or 0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(commands, [])
        self.assertIn("No changed files detected. All targets are up to date.", out.getvalue())

    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.run_bazel_query")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_publish_core_file_escalation(
        self,
        mock_changed: mock.MagicMock,
        mock_query: mock.MagicMock,
        mock_diff: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["MODULE.bazel"]
        mock_query.return_value = ["//src/infra:publish", "//src/examples:publish"]
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_publish(
            self.repo_root,
            ParsedArgs(subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 2)
        self.assertIn("//src/examples:publish", commands[0])
        self.assertIn("//src/infra:publish", commands[1])
        expected_query = (
            'kind(".*", //src/examples/... + //src/infra/... + //src/third_party/...) intersect'
            ' attr("name", "publish", //...)'
        )
        mock_query.assert_called_once_with(self.repo_root, expected_query)

        fail_runner = mock.MagicMock(return_value=3)
        code_fail = run_publish(
            self.repo_root,
            ParsedArgs(subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fail_runner,
        )
        self.assertEqual(code_fail, 3)

        mock_query.return_value = []
        out = io.StringIO()
        with redirect_stdout(out):
            code_empty = run_publish(
                self.repo_root,
                ParsedArgs(
                    subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]
                ),
                runner=fake_runner,
            )
        self.assertEqual(code_empty, 0)
        self.assertIn("No image publish targets found.", out.getvalue())

        mock_changed.return_value = []
        mock_query.return_value = ["//src/infra:publish"]
        commands.clear()
        code_all = run_publish(
            self.repo_root,
            ParsedArgs(subcommand="publish", run_all=True, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )
        self.assertEqual(code_all, 0)
        self.assertEqual(len(commands), 1)

        mock_changed.return_value = ["some/file.txt"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=["//..."],
            affected_targets=["//..."],
            affected_tests=["//..."],
            affected_packages=["//..."],
            is_global=True,
        )
        commands.clear()
        code_global = run_publish(
            self.repo_root,
            ParsedArgs(subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )
        self.assertEqual(code_global, 0)
        self.assertEqual(len(commands), 1)

    @mock.patch("src.bazel.tools.diff.cli.run_bazel_query")
    @mock.patch("src.bazel.tools.diff.cli.get_bazel_diff")
    @mock.patch("src.bazel.tools.diff.cli.get_changed_files")
    def test_run_publish_affected_targets(
        self,
        mock_changed: mock.MagicMock,
        mock_diff: mock.MagicMock,
        mock_query: mock.MagicMock,
    ) -> None:
        mock_changed.return_value = ["src/infra/docker/Dockerfile"]
        mock_diff.return_value = BazelDiffResult(
            direct_targets=["//src/infra/docker:image"],
            affected_targets=[],
            affected_tests=[],
            affected_packages=["//src/infra/docker:..."],
            is_global=False,
        )
        mock_query.return_value = ["//src/infra/docker:publish"]
        commands: list[Sequence[str]] = []

        def fake_runner(cmd: Sequence[str]) -> int:
            commands.append(cmd)
            return 0

        code = run_publish(
            self.repo_root,
            ParsedArgs(subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("//src/infra/docker:publish", commands[0])
        expected_query = (
            'kind(".*", rdeps(//..., set(//src/infra/docker:image), 10)) intersect'
            ' attr("name", "publish", //...)'
        )
        mock_query.assert_called_once_with(self.repo_root, expected_query)

        fail_runner = mock.MagicMock(return_value=2)
        code_fail = run_publish(
            self.repo_root,
            ParsedArgs(subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]),
            runner=fail_runner,
        )
        self.assertEqual(code_fail, 2)

        mock_diff.return_value = BazelDiffResult(
            direct_targets=[],
            affected_targets=[],
            affected_tests=[],
            affected_packages=[],
            is_global=False,
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code_no_direct = run_publish(
                self.repo_root,
                ParsedArgs(
                    subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]
                ),
                runner=fake_runner,
            )
        self.assertEqual(code_no_direct, 0)
        self.assertIn(
            "No changed image targets detected. All targets are up to date.", out.getvalue()
        )

        mock_diff.return_value = BazelDiffResult(
            direct_targets=["//src/infra/docker:image"],
            affected_targets=[],
            affected_tests=[],
            affected_packages=["//src/infra/docker:..."],
            is_global=False,
        )
        mock_query.return_value = []
        out = io.StringIO()
        with redirect_stdout(out):
            code_no_publish = run_publish(
                self.repo_root,
                ParsedArgs(
                    subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]
                ),
                runner=fake_runner,
            )
        self.assertEqual(code_no_publish, 0)
        self.assertIn(
            "No changed image targets detected. All targets are up to date.", out.getvalue()
        )

    @mock.patch("src.bazel.tools.diff.cli.run_publish")
    def test_dispatch_publish(self, mock_run_publish: mock.MagicMock) -> None:
        mock_run_publish.return_value = 0
        parsed = ParsedArgs(
            subcommand="publish", run_all=False, explicit_targets=[], bazel_flags=[]
        )
        code = dispatch(self.repo_root, parsed)
        self.assertEqual(code, 0)
        mock_run_publish.assert_called_once()

    def test_dispatch_invalid_subcommand(self) -> None:
        with self.assertRaises(ValueError):
            dispatch(
                self.repo_root,
                ParsedArgs(
                    subcommand="invalid", run_all=False, explicit_targets=[], bazel_flags=[]
                ),
            )

    @mock.patch("src.bazel.tools.diff.cli.dispatch")
    def test_main_entrypoint(self, mock_dispatch: mock.MagicMock) -> None:
        mock_dispatch.return_value = 0
        code = main(["cli.py", "test", "--all"])
        self.assertEqual(code, 0)
        mock_dispatch.assert_called_once()
