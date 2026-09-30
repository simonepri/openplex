"""Command-line interface for diff-aware Bazel test, check, fix, and publish execution.

Resolves changed files, evaluates affected targets and packages, and dispatches Bazel commands.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

try:
    from src.bazel.tools.diff.bazel_diff import (
        CORE_FILES,
        get_bazel_diff,
        normalize_target_pattern,
        run_bazel_query,
    )
    from src.bazel.tools.diff.gate_diff import GATE_TRIGGERS, filter_gates, filter_generators
    from src.bazel.tools.diff.git_diff import get_changed_files, is_ci_push_to_main
except ImportError:  # pragma: no cover
    try:
        from .bazel_diff import (  # type: ignore[no-redef]
            CORE_FILES,
            get_bazel_diff,
            normalize_target_pattern,
            run_bazel_query,
        )
        from .gate_diff import (  # type: ignore[no-redef]
            GATE_TRIGGERS,
            filter_gates,
            filter_generators,
        )
        from .git_diff import get_changed_files, is_ci_push_to_main  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        from bazel_diff import (  # type: ignore[no-redef]
            CORE_FILES,
            get_bazel_diff,
            normalize_target_pattern,
            run_bazel_query,
        )
        from gate_diff import (  # type: ignore[no-redef]
            GATE_TRIGGERS,
            filter_gates,
            filter_generators,
        )
        from git_diff import get_changed_files, is_ci_push_to_main  # type: ignore[no-redef]

FLAGS_WITH_VALUES: frozenset[str] = frozenset({
    "-c",
    "--compilation_mode",
    "--test_filter",
    "--test_tag_filters",
    "--build_tag_filters",
    "--test_timeout",
    "--test_arg",
    "--output_user_root",
})


@dataclasses.dataclass(frozen=True)
class ParsedArgs:
    """Parsed CLI arguments separating subcommand, control flags, targets, and Bazel options."""

    subcommand: str
    run_all: bool
    explicit_targets: list[str]
    bazel_flags: list[str]


def parse_cli_args(args: Sequence[str], repo_root: Path | None = None) -> ParsedArgs:
    """Parse command-line arguments into subcommand, control flags, targets, and pass-through flags."""
    root = repo_root or Path.cwd()
    if not args:
        return ParsedArgs(
            subcommand="test", run_all=is_ci_push_to_main(), explicit_targets=[], bazel_flags=[]
        )

    subcommand = args[0] if args[0] in {"test", "check", "fix", "publish"} else "test"
    rest = list(args[1:]) if args[0] in {"test", "check", "fix", "publish"} else list(args)

    run_all = is_ci_push_to_main()
    explicit_targets: list[str] = []
    bazel_flags: list[str] = []

    idx = 0
    while idx < len(rest):
        arg = rest[idx]

        if arg == "--":
            for target in rest[idx + 1 :]:
                explicit_targets.append(normalize_target_pattern(root, target))
            break

        if arg in {"--all", "-a"}:
            run_all = True
            idx += 1
            continue

        if arg.startswith("-"):
            bazel_flags.append(arg)
            if (
                arg in FLAGS_WITH_VALUES
                and (idx + 1) < len(rest)
                and not rest[idx + 1].startswith("-")
            ):
                idx += 1
                bazel_flags.append(rest[idx])
            idx += 1
            continue

        explicit_targets.append(normalize_target_pattern(root, arg))
        idx += 1

    return ParsedArgs(
        subcommand=subcommand,
        run_all=run_all,
        explicit_targets=explicit_targets,
        bazel_flags=bazel_flags,
    )


def run_system_command(cmd: Sequence[str], cwd: Path | None = None) -> int:
    """Execute a system command and return the exit code."""
    workspace_dir = cwd or (
        Path(os.environ["BUILD_WORKSPACE_DIRECTORY"])
        if "BUILD_WORKSPACE_DIRECTORY" in os.environ
        else None
    )
    res = subprocess.run(cmd, cwd=workspace_dir, check=False)
    return res.returncode


def _get_output_flags(bazel_flags: Sequence[str]) -> list[str]:
    """Return the BAZEL_OUTPUT_ROOT --output_user_root flag unless the caller passed one.

    Without BAZEL_OUTPUT_ROOT, Bazel's default root applies, so commands share
    the server that plain `bazel` starts instead of killing it.
    """
    output_root = os.environ.get("BAZEL_OUTPUT_ROOT")
    if not output_root or any(flag.startswith("--output_user_root") for flag in bazel_flags):
        return []
    return [f"--output_user_root={output_root}"]


def run_test(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[Sequence[str]], int] = run_system_command,
) -> int:
    """Execute test subcommand based on explicit targets or affected workspace changes."""
    output_flags = _get_output_flags(parsed.bazel_flags)
    targets = (
        ["//..."]
        if (parsed.run_all and not parsed.explicit_targets)
        else list(parsed.explicit_targets)
    )

    if targets:
        cmd = ["bazel", *output_flags, "test", *parsed.bazel_flags, "--", *targets]
        return runner(cmd)

    changed_files = get_changed_files(repo_root)
    if not changed_files:
        print("No changed files detected. All targets are up to date.")
        return 0

    diff_res = get_bazel_diff(repo_root, changed_files)
    if not diff_res.affected_tests:
        print("No affected tests detected for changed files.")
        return 0

    cmd = ["bazel", *output_flags, "test", *parsed.bazel_flags, "--", *diff_res.affected_tests]
    return runner(cmd)


def run_check(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[Sequence[str]], int] = run_system_command,
) -> int:
    """Execute check subcommand with full repository, target-scoped, or diff-filtered packages."""
    output_flags = _get_output_flags(parsed.bazel_flags)

    if parsed.run_all:
        cmd = ["bazel", *output_flags, "run", *parsed.bazel_flags, "//:check", "--", "--all"]
        return runner(cmd)

    if parsed.explicit_targets:
        cmd = [
            "bazel",
            *output_flags,
            "run",
            *parsed.bazel_flags,
            "//:check",
            "--",
            *parsed.explicit_targets,
        ]
        return runner(cmd)

    changed_files = get_changed_files(repo_root)
    if not changed_files:
        print("No changed files detected. All checks passed.")
        return 0

    diff_res = get_bazel_diff(repo_root, changed_files)
    if diff_res.is_global or any(f in CORE_FILES for f in changed_files):
        filtered_args = ["--all"]
    elif diff_res.affected_packages:
        filtered_args = list(diff_res.affected_packages)
    else:
        triggered_gates = filter_gates(
            list(GATE_TRIGGERS.keys()), changed_files, repo_root=repo_root
        )
        if not triggered_gates:
            print("No changed files detected. All checks passed.")
            return 0
        filtered_args = []

    if filtered_args:
        cmd = ["bazel", *output_flags, "run", *parsed.bazel_flags, "//:check", "--", *filtered_args]
    else:
        cmd = ["bazel", *output_flags, "run", *parsed.bazel_flags, "//:check"]
    return runner(cmd)


def run_fix(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[Sequence[str]], int] = run_system_command,
) -> int:
    """Execute fix subcommand passing through --all or targets or invoking affected fix generators."""
    output_flags = _get_output_flags(parsed.bazel_flags)

    if parsed.run_all:
        cmd = ["bazel", *output_flags, "run", *parsed.bazel_flags, "//:fix", "--", "--all"]
        return runner(cmd)

    if parsed.explicit_targets:
        cmd = [
            "bazel",
            *output_flags,
            "run",
            *parsed.bazel_flags,
            "//:fix",
            "--",
            *parsed.explicit_targets,
        ]
        return runner(cmd)

    changed_files = get_changed_files(repo_root)
    if not changed_files:
        print("No changed files detected. All targets are up to date.")
        return 0

    active_generators = filter_generators(changed_files)
    diff_res = get_bazel_diff(repo_root, changed_files)
    if diff_res.is_global or any(f in CORE_FILES for f in changed_files):
        cmd = ["bazel", *output_flags, "run", *parsed.bazel_flags, "//:fix", "--", "--all"]
    elif diff_res.affected_packages:
        cmd = [
            "bazel",
            *output_flags,
            "run",
            *parsed.bazel_flags,
            "//:fix",
            "--",
            *diff_res.affected_packages,
        ]
    else:
        if not active_generators:
            print("No changed files detected. All targets are up to date.")
            return 0
        cmd = ["bazel", *output_flags, "run", *parsed.bazel_flags, "//:fix"]
    return runner(cmd)


def _publish_targets(
    targets: Sequence[str],
    output_flags: Sequence[str],
    bazel_flags: Sequence[str],
    runner: Callable[[Sequence[str]], int],
) -> int:
    for target in targets:
        rc = runner(["bazel", *output_flags, "run", *bazel_flags, target])
        if rc != 0:
            return rc
    return 0


def _publish_all_targets(
    repo_root: Path,
    output_flags: Sequence[str],
    bazel_flags: Sequence[str],
    runner: Callable[[Sequence[str]], int],
) -> int:
    """Execute all publish targets across examples, infra, and third-party."""
    query_expr = (
        'kind(".*", //src/examples/... + //src/infra/... + //src/third_party/...) intersect'
        ' attr("name", "publish", //...)'
    )
    targets = sorted(run_bazel_query(repo_root, query_expr))
    if not targets:
        print("No image publish targets found.")
        return 0
    return _publish_targets(targets, output_flags, bazel_flags, runner)


def run_publish(
    repo_root: Path,
    parsed: ParsedArgs,
    runner: Callable[[Sequence[str]], int] = run_system_command,
) -> int:
    """Execute publish subcommand for explicit targets, repository all, or affected image targets."""
    output_flags = _get_output_flags(parsed.bazel_flags)

    if parsed.explicit_targets:
        return _publish_targets(parsed.explicit_targets, output_flags, parsed.bazel_flags, runner)

    changed_files = get_changed_files(repo_root)
    if not changed_files and not parsed.run_all:
        print("No changed files detected. All targets are up to date.")
        return 0

    diff_res = get_bazel_diff(repo_root, changed_files)
    if parsed.run_all or any(f in CORE_FILES for f in changed_files) or diff_res.is_global:
        return _publish_all_targets(repo_root, output_flags, parsed.bazel_flags, runner)

    if not diff_res.direct_targets:
        print("No changed image targets detected. All targets are up to date.")
        return 0

    target_set = " ".join(diff_res.direct_targets)
    query_expr = (
        f'kind(".*", rdeps(//..., set({target_set}), 10)) intersect attr("name", "publish", //...)'
    )
    targets = sorted(run_bazel_query(repo_root, query_expr))
    if not targets:
        print("No changed image targets detected. All targets are up to date.")
        return 0

    return _publish_targets(targets, output_flags, parsed.bazel_flags, runner)


def dispatch(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[Sequence[str]], int] = run_system_command,
) -> int:
    """Dispatch the parsed subcommand to test, check, fix, or publish logic."""
    if parsed.subcommand == "test":
        return run_test(repo_root, parsed, runner=runner)
    if parsed.subcommand == "check":
        return run_check(repo_root, parsed, runner=runner)
    if parsed.subcommand == "fix":
        return run_fix(repo_root, parsed, runner=runner)
    if parsed.subcommand == "publish":
        return run_publish(repo_root, parsed, runner=runner)

    raise ValueError(f"Unknown subcommand: {parsed.subcommand}")


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entrypoint for diff-aware execution."""
    raw_args = list(argv[1:] if argv is not None else sys.argv[1:])
    repo_root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd())).resolve()
    parsed = parse_cli_args(raw_args, repo_root=repo_root)
    return dispatch(repo_root, parsed)


if __name__ == "__main__":
    sys.exit(main())
