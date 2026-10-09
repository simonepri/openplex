"""Command-line interface for diff-aware Bazel target and argument resolution.

Resolves changed files, evaluates affected targets and packages, and prints them to stdout.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

try:
    from src.bazel.tools.diff.bazel_diff import (
        CORE_FILES,
        get_bazel_diff,
        normalize_target_pattern,
        run_bazel_query,
    )
    from src.bazel.tools.diff.gate_diff import (
        GATE_TRIGGERS,
        filter_gates,
        filter_generators,
        is_core_file,
    )
    from src.bazel.tools.diff.git_diff import (
        detect_git_baseline,
        get_changed_files,
    )
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
            is_core_file,
        )
        from .git_diff import (  # type: ignore[no-redef]
            detect_git_baseline,
            get_changed_files,
        )
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
            is_core_file,
        )
        from git_diff import (  # type: ignore[no-redef]
            detect_git_baseline,
            get_changed_files,
        )

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
        return ParsedArgs(subcommand="test", run_all=False, explicit_targets=[], bazel_flags=[])

    subcommand = (
        args[0] if args[0] in {"test", "targets", "build", "check", "fix", "publish"} else "test"
    )
    rest = (
        list(args[1:])
        if args[0] in {"test", "targets", "build", "check", "fix", "publish"}
        else list(args)
    )

    run_all = False
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


def resolve_test_targets(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    diff_fn: Callable[..., Any] | None = None,
    query_fn: Callable[..., list[str]] | None = None,
) -> list[str]:
    """Resolve test targets affected by workspace changes or explicit patterns."""
    targets = (
        ["//..."]
        if (parsed.run_all and not parsed.explicit_targets)
        else list(parsed.explicit_targets)
    )

    if targets:
        return targets

    base_ref, _ = detect_git_baseline(repo_root)
    if not base_ref:
        print(
            "[INFO] No git baseline detected; running full test suite",
            file=sys.stderr,
        )
        return ["//..."]

    changed_files = get_changed_files(repo_root, base_ref=base_ref)
    if not changed_files:
        return []

    if any(f in CORE_FILES for f in changed_files):
        print("[INFO] Toolchain files modified; running full test suite", file=sys.stderr)
        return ["//..."]

    run_query = query_fn or run_bazel_query
    get_diff = diff_fn or get_bazel_diff
    diff_res = get_diff(repo_root, changed_files, query_fn=run_query)
    candidates = diff_res.affected_tests
    selected_tests: list[str] = []

    # Explicit labels make `bazel test` run tests tagged manual, which `//...` skips.
    if candidates:
        target_set = " ".join(f'"{c}"' for c in candidates)
        selected_tests = run_query(
            repo_root,
            f"kind('.*_test', set({target_set})) except attr(tags, '\\\\bmanual\\\\b', set({target_set}))",
            bazel_output_root=os.environ.get("BAZEL_OUTPUT_ROOT"),
        )

    print(
        f"[INFO] Using bazel query engine (base commit: {base_ref}); selected {len(selected_tests)} test(s)",
        file=sys.stderr,
    )
    return selected_tests


def resolve_build_targets(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    diff_fn: Callable[..., Any] | None = None,
    query_fn: Callable[..., list[str]] | None = None,
) -> list[str]:
    """Resolve build targets affected by workspace changes or explicit patterns."""
    targets = (
        ["//..."]
        if (parsed.run_all and not parsed.explicit_targets)
        else list(parsed.explicit_targets)
    )

    if targets:
        return targets

    base_ref, _ = detect_git_baseline(repo_root)
    if not base_ref:
        print(
            "[INFO] No git baseline detected; building full codebase",
            file=sys.stderr,
        )
        return ["//..."]

    changed_files = get_changed_files(repo_root, base_ref=base_ref)
    if not changed_files:
        return []

    if any(f in CORE_FILES for f in changed_files):
        print("[INFO] Toolchain files modified; building full codebase", file=sys.stderr)
        return ["//..."]

    run_query = query_fn or run_bazel_query
    get_diff = diff_fn or get_bazel_diff
    diff_res = get_diff(repo_root, changed_files, query_fn=run_query)
    candidates = diff_res.affected_targets
    selected_targets: list[str] = []

    if candidates:
        target_set = " ".join(f'"{c}"' for c in candidates)
        selected_targets = run_query(
            repo_root,
            f"set({target_set}) except kind('source file', set({target_set})) except attr(tags, '\\\\bmanual\\\\b', set({target_set}))",
            bazel_output_root=os.environ.get("BAZEL_OUTPUT_ROOT"),
        )

    print(
        f"[INFO] Using bazel query engine (base commit: {base_ref}); selected {len(selected_targets)} target(s)",
        file=sys.stderr,
    )
    return selected_targets


def run_targets(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[list[str]], int] | None = None,
    diff_fn: Callable[..., Any] | None = None,
    query_fn: Callable[..., list[str]] | None = None,
) -> int:
    """Resolve and output affected build targets."""
    selected_targets = resolve_build_targets(
        repo_root,
        parsed,
        diff_fn=diff_fn,
        query_fn=query_fn,
    )
    if not selected_targets:
        return 0

    if runner is not None:
        return runner(selected_targets)

    for target in selected_targets:
        print(target)
    return 0


def run_test(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[list[str]], int] | None = None,
    diff_fn: Callable[..., Any] | None = None,
    query_fn: Callable[..., list[str]] | None = None,
) -> int:
    """Resolve test targets and print them to stdout or forward to runner."""
    selected_tests = resolve_test_targets(repo_root, parsed, diff_fn=diff_fn, query_fn=query_fn)
    if not selected_tests:
        return 0

    if runner is not None:
        return runner(selected_tests)

    for target in selected_tests:
        print(target)
    return 0


def resolve_check_targets(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    diff_fn: Callable[..., Any] | None = None,
) -> list[str]:
    """Resolve check arguments/packages based on explicit targets or affected workspace changes."""
    if parsed.run_all:
        return ["--all"]
    if parsed.explicit_targets:
        return list(parsed.explicit_targets)

    changed_files = get_changed_files(repo_root)
    if not changed_files:
        return []

    get_diff = diff_fn or get_bazel_diff
    diff_res = get_diff(repo_root, changed_files)
    core_changed = diff_res.is_global or any(is_core_file(f) for f in changed_files)
    affected_packages: list[str] = [
        str(pkg) for pkg in diff_res.affected_packages if pkg != "//..."
    ]

    if core_changed:
        return ["--all"]
    if affected_packages:
        return affected_packages

    has_triggered_gates = bool(
        filter_gates(list(GATE_TRIGGERS.keys()), changed_files, repo_root=repo_root)
    )
    return ["--all"] if has_triggered_gates else []


def run_check(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[list[str]], int] | None = None,
    diff_fn: Callable[..., Any] | None = None,
) -> int:
    """Resolve check arguments/packages and print them to stdout or forward to runner."""
    targets = resolve_check_targets(repo_root, parsed, diff_fn=diff_fn)
    if not targets:
        return 0

    if runner is not None:
        return runner(targets)

    for target in targets:
        print(target)
    return 0


def resolve_fix_targets(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    diff_fn: Callable[..., Any] | None = None,
) -> list[str]:
    """Resolve fix arguments/packages based on explicit targets or affected workspace changes."""
    if parsed.run_all:
        return ["--all"]
    if parsed.explicit_targets:
        return list(parsed.explicit_targets)

    changed_files = get_changed_files(repo_root)
    if not changed_files:
        return []

    active_generators = filter_generators(changed_files)
    get_diff = diff_fn or get_bazel_diff
    diff_res = get_diff(repo_root, changed_files)
    core_changed = diff_res.is_global or any(is_core_file(f) for f in changed_files)
    affected_packages: list[str] = [
        str(pkg) for pkg in diff_res.affected_packages if pkg != "//..."
    ]

    if core_changed:
        return ["--all"]
    if affected_packages:
        return affected_packages
    return ["--all"] if active_generators else []


def run_fix(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[list[str]], int] | None = None,
    diff_fn: Callable[..., Any] | None = None,
) -> int:
    """Resolve fix arguments/packages and print them to stdout or forward to runner."""
    targets = resolve_fix_targets(repo_root, parsed, diff_fn=diff_fn)
    if not targets:
        return 0

    full_args = list(parsed.bazel_flags) + targets
    if runner is not None:
        return runner(full_args)

    for arg in full_args:
        print(arg)
    return 0


def resolve_publish_targets(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    diff_fn: Callable[..., Any] | None = None,
    query_fn: Callable[..., list[str]] | None = None,
) -> list[str]:
    """Resolve image publish targets based on explicit targets, repository all, or affected changes."""
    if parsed.explicit_targets:
        return list(parsed.explicit_targets)

    run_query = query_fn or run_bazel_query
    changed_files = get_changed_files(repo_root)
    if not changed_files and not parsed.run_all:
        return []

    get_diff = diff_fn or get_bazel_diff
    diff_res = get_diff(repo_root, changed_files)
    if parsed.run_all or any(is_core_file(f) for f in changed_files) or diff_res.is_global:
        query_expr = (
            'kind(".*", //src/examples/... + //src/infra/... + //src/third_party/...) intersect'
            ' attr("name", "publish", //...)'
        )
        return sorted(run_query(repo_root, query_expr))

    if not diff_res.direct_targets:
        return []

    target_set = " ".join(f'"{t}"' for t in diff_res.direct_targets)
    query_expr = (
        f'kind(".*", rdeps(//..., set({target_set}), 10)) intersect attr("name", "publish", //...)'
    )
    return sorted(run_query(repo_root, query_expr))


def run_publish(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[list[str]], int] | None = None,
    diff_fn: Callable[..., Any] | None = None,
    query_fn: Callable[..., list[str]] | None = None,
) -> int:
    """Resolve publish targets and print them to stdout or forward to runner."""
    targets = resolve_publish_targets(repo_root, parsed, diff_fn=diff_fn, query_fn=query_fn)
    if not targets:
        return 0

    if runner is not None:
        return runner(targets)

    for target in targets:
        print(target)
    return 0


def dispatch(
    repo_root: Path,
    parsed: ParsedArgs,
    *,
    runner: Callable[[list[str]], int] | None = None,
) -> int:
    """Dispatch the parsed subcommand to test, build, targets, check, fix, or publish target resolution."""
    if parsed.subcommand == "test":
        return run_test(repo_root, parsed, runner=runner)
    if parsed.subcommand in {"targets", "build"}:
        return run_targets(repo_root, parsed, runner=runner)
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
