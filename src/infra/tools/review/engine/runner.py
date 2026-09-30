"""Orchestrates the PR review workflow across diff extraction, dependency analysis, rubric synthesis, and scoring."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
from typing import TYPE_CHECKING, Any

from src.infra.tools.review.context.bazel_diff import BazelDiffResult, get_bazel_diff
from src.infra.tools.review.context.codegraph import CodeGraphResult, get_codegraph_analysis
from src.infra.tools.review.context.git_diff import get_git_diff
from src.infra.tools.review.context.rules_index import diff_rules, get_applicable_rules
from src.infra.tools.review.engine.agents import DEFAULT_REGISTRY
from src.infra.tools.review.engine.matrix import evaluate_report_contracts
from src.infra.tools.review.engine.prompt import compile_review_prompt
from src.infra.tools.review.engine.report import compute_size_bucket, parse_review
from src.infra.tools.review.engine.rubrics import synthesize_rubrics

if TYPE_CHECKING:
    from pathlib import Path


@dataclasses.dataclass(frozen=True)
class ReviewBundle:
    """Compiled review package."""

    prompt: str
    base_ref: str
    target_ref: str
    files_changed: int
    lines_changed: int = 0
    size_bucket: str = "M"


def build_review_bundle(
    repo_root: Path,
    base: str | None = None,
    target: str | None = None,
    *,
    include_codegraph: bool = True,
    include_bazel: bool = True,
) -> ReviewBundle:
    """Gathers all repository context and compiles the review prompt bundle."""
    # 1. Git Diff
    diff_res = get_git_diff(repo_root, base=base, target=target)

    # 2. Bazel Diff
    if include_bazel and diff_res.changed_paths:
        bazel_res = get_bazel_diff(repo_root, diff_res.changed_paths)
    else:
        bazel_res = BazelDiffResult(direct_targets=[], affected_targets=[], affected_tests=[])

    # 3. CodeGraph Impact
    if include_codegraph and diff_res.changed_paths:
        cg_res = get_codegraph_analysis(repo_root, diff_res.changed_paths)
    else:
        cg_res = CodeGraphResult(affected_tests=[], impacted_symbols=[], context_markdown="")

    # 4. Applicable Rules & Rule Diffs
    rules_res = get_applicable_rules(repo_root, diff_res.changed_paths)
    rule_diff_res = diff_rules(repo_root, diff_res.base_ref, diff_res.target_ref)

    # 5. Matrices & Report Artifacts
    matrices = evaluate_report_contracts(repo_root, rules_res.report_contracts)

    # 6. Dynamic Rubrics
    rubrics = synthesize_rubrics(rules_res.applicable_rules)

    # 7. Prompt Assembly
    prompt = compile_review_prompt(
        diff=diff_res,
        bazel_diff=bazel_res,
        codegraph=cg_res,
        rubrics=rubrics,
        rule_diff=rule_diff_res,
        matrices=matrices,
    )

    total_lines = diff_res.total_additions + diff_res.total_deletions
    size_bucket = compute_size_bucket(len(diff_res.files), total_lines)

    return ReviewBundle(
        prompt=prompt,
        base_ref=diff_res.base_ref,
        target_ref=diff_res.target_ref,
        files_changed=len(diff_res.files),
        lines_changed=total_lines,
        size_bucket=size_bucket,
    )


def _extract_review_payload(raw_output: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw_output)
        if isinstance(parsed, dict):
            if "structured_output" in parsed and isinstance(parsed["structured_output"], dict):
                return parsed["structured_output"]
            if "result" in parsed:
                res = parsed["result"]
                if isinstance(res, dict):
                    return res
                if isinstance(res, str):
                    with contextlib.suppress(Exception):
                        return json.loads(res)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
    if match:
        with contextlib.suppress(Exception):
            return json.loads(match.group(1))

    match_brace = re.search(r"(\{.*\})", raw_output, re.DOTALL)
    if match_brace:
        with contextlib.suppress(Exception):
            return json.loads(match_brace.group(1))

    return None


def _extract_review_metadata(raw_output: str, provider: str, model_hint: str) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "provider": provider,
        "model": model_hint,
    }
    with contextlib.suppress(Exception):
        parsed = json.loads(raw_output)
        if isinstance(parsed, dict):
            if "total_cost_usd" in parsed:
                meta["cost"] = parsed.get("total_cost_usd", 0.0)
            if "duration_ms" in parsed:
                meta["duration_ms"] = parsed.get("duration_ms", 0)
            if "usage" in parsed and isinstance(parsed["usage"], dict):
                usage = parsed["usage"]
                meta["tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return meta


def invoke_review_agent(
    prompt: str, agent_cmd: str, repo_root: Path, size_bucket: str = "M"
) -> str:
    """Feeds prompt into a registered agent CLI and persists structured review results."""
    review_dir = repo_root / ".review"
    review_dir.mkdir(exist_ok=True)
    bundle_file = review_dir / "review-bundle.md"
    bundle_file.write_text(prompt, encoding="utf-8")

    out_file = review_dir / "review-result.json"
    meta_file = review_dir / "review-meta.json"
    err_file = review_dir / "review-error.txt"

    plugin, custom_args = DEFAULT_REGISTRY.resolve(agent_cmd)
    agent_env = plugin.configure_environment()

    exec_res = plugin.run(
        bundle_file=bundle_file,
        repo_root=repo_root,
        env=agent_env,
        custom_args=custom_args,
        size_bucket=size_bucket,
    )

    if exec_res.returncode != 0:
        err_msg = (
            f"Agent '{plugin.name}' returned non-zero exit code ({exec_res.returncode}):\n"
            f"{exec_res.stderr}\n{exec_res.raw_output}"
        )
        err_file.write_text(err_msg, encoding="utf-8")
        return err_msg

    review_data = _extract_review_payload(exec_res.raw_output)
    model_hint = exec_res.model_used or plugin.name
    meta_data = _extract_review_metadata(exec_res.raw_output, plugin.provider, model_hint)

    if review_data is not None:
        try:
            parse_review(review_data)
            out_file.write_text(json.dumps(review_data, indent=2), encoding="utf-8")
            meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")
        except Exception as e:
            err_file.write_text(
                f"Invalid review schema: {e}\nRaw output: {exec_res.raw_output}", encoding="utf-8"
            )
    else:
        err_file.write_text(
            f"Could not parse review output from agent '{plugin.name}'.\nOutput:\n{exec_res.raw_output}",
            encoding="utf-8",
        )

    return exec_res.raw_output
