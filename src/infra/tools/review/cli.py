"""Provides the command-line interface entry point for executing automated agentic pull request code reviews."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.infra.tools.review.engine.github_comment import main as comment_main
from src.infra.tools.review.engine.runner import build_review_bundle, invoke_review_agent


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:]) if argv is None else argv
    if raw_args and raw_args[0] == "comment":
        return comment_main(raw_args[1:])

    parser = argparse.ArgumentParser(
        prog="review",
        description="Comprehensive repository PR review pipeline driving Git diff, Bazel queries, CodeGraph, and RuleSync rules.",
    )
    parser.add_argument(
        "--base",
        type=str,
        default=None,
        help="Base git reference (e.g. main, HEAD~1, origin/main). Defaults to auto-detect.",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target git reference or WORKTREE. Defaults to auto-detect.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Path to write the review bundle markdown. If omitted, writes to .review/review-bundle.md.",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default=None,
        help="Agent command to invoke with the review bundle (e.g. 'claude').",
    )
    parser.add_argument(
        "--no-codegraph",
        action="store_true",
        help="Skip CodeGraph symbol impact and context gathering.",
    )
    parser.add_argument(
        "--no-bazel",
        action="store_true",
        help="Skip Bazel dependency graph queries.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print review bundle to stdout instead of only writing to file.",
    )

    args = parser.parse_args(argv)

    # Locate repo root
    current = Path.cwd()
    repo_root = current
    while repo_root != repo_root.parent:
        if (repo_root / "MODULE.bazel").exists() or (repo_root / ".git").exists():
            break
        repo_root = repo_root.parent

    print(
        f"Gathering review context (base: {args.base or 'auto'}, target: {args.target or 'auto'})...",
        file=sys.stderr,
    )
    bundle = build_review_bundle(
        repo_root=repo_root,
        base=args.base,
        target=args.target,
        include_codegraph=not args.no_codegraph,
        include_bazel=not args.no_bazel,
    )

    out_path = Path(args.output) if args.output else (repo_root / ".review" / "review-bundle.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(bundle.prompt, encoding="utf-8")
    print(
        f"Review bundle written to {out_path} ({bundle.files_changed} files changed, {bundle.lines_changed} lines, size: [{bundle.size_bucket}])",
        file=sys.stderr,
    )

    if args.stdout:
        print(bundle.prompt)

    if args.agent:
        print(
            f"Invoking review agent: {args.agent} (PR size: [{bundle.size_bucket}])...",
            file=sys.stderr,
        )
        agent_out = invoke_review_agent(
            bundle.prompt, args.agent, repo_root, size_bucket=bundle.size_bucket
        )
        print(agent_out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
