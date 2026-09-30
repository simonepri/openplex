"""Manages the GitHub sticky comment lifecycle for pull request reviews, tracking review progress and merge gates."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

from src.infra.tools.review.engine.report import effective_verdict, parse_review, render_report

STICKY_MARKER = "<!-- code-review-sticky -->"
REVIEW_OPEN = "<!-- review-start -->"
REVIEW_CLOSE = "<!-- review-end -->"
SPINNER = (
    '<img src="https://github.com/user-attachments/assets/5ac382c7-e004-429b-8e35-7feb3e8f9c6f" '
    'width="14px" height="14px" style="vertical-align: middle" />'
)


@dataclasses.dataclass(frozen=True)
class ReviewStats:
    """Cumulative and per-run review metrics."""

    runs: int = 0
    tokens: int = 0
    cost: float = 0.0
    tokens_available: bool = True
    cost_available: bool = True


def set_github_output(name: str, value: str) -> None:
    """Appends an output key-value pair to GITHUB_OUTPUT environment file."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def parse_override_from_body(pr_body: str | None) -> tuple[bool, str]:
    """Detects NO_LGTM=<reason> override in the PR description."""
    if not pr_body:
        return False, ""
    match = re.search(r"(?m)^\s*NO_LGTM=(.*)$", pr_body)
    if not match:
        return False, ""
    reason = str(match.group(1)).strip()
    if not reason or reason == "<reason>":
        return False, ""
    return True, reason


def extract_previous_review(body: str) -> str:
    """Extracts content between review markers in a comment body."""
    start_idx = body.find(REVIEW_OPEN)
    if start_idx == -1:
        return ""
    start_idx += len(REVIEW_OPEN)
    end_idx = body.find(REVIEW_CLOSE, start_idx)
    if end_idx == -1:
        return ""
    return body[start_idx:end_idx].strip()


def parse_stats_marker(body: str) -> ReviewStats:
    """Parses cumulative metrics from the hidden HTML marker."""
    match = re.search(
        r"<!-- review-stats runs=(\d+) tokens=(\d+)(?: cost=([\d.]+))?"
        r"(?: tokens_available=(true|false))?(?: cost_available=(true|false))? -->",
        body,
    )
    if not match:
        return ReviewStats()

    runs = int(match.group(1))
    tokens = int(match.group(2))
    cost = float(match.group(3)) if match.group(3) else 0.0
    tokens_avail = match.group(4) != "false"
    cost_avail = match.group(5) != "false"
    return ReviewStats(
        runs=runs,
        tokens=tokens,
        cost=cost,
        tokens_available=tokens_avail,
        cost_available=cost_avail,
    )


def format_duration(duration_ms: int) -> str:
    """Formats milliseconds as mm:ss or seconds."""
    secs = max(0, duration_ms // 1000)
    if secs >= 60:
        return f"{secs // 60}m{secs % 60}s"
    return f"{secs}s"


def format_tokens(tokens: int, *, available: bool = True) -> str:
    """Formats token count compactly (e.g. 45k, 1.2M)."""
    if not available:
        return "n/a"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens // 1_000}k"
    return str(tokens)


def format_cost(cost: float, *, available: bool = True) -> str:
    """Formats dollar cost."""
    if not available:
        return "n/a"
    return f"${cost:.2f}"


def build_collapsed_previous(previous_review: str) -> str:
    """Wraps previous review in <details> tag with extracted verdict."""
    if not previous_review:
        return ""
    match = re.search(
        r"(LGTM|CHANGES REQUESTED|DO NOT MERGE)\s+\[[A-Z]+\]", previous_review, re.IGNORECASE
    )
    verdict_suffix = f" — {match.group(0)}" if match else ""
    return (
        f"\n---\n\n<details>\n<summary>📋 Previous review{verdict_suffix}</summary>\n<br>\n\n"
        f"{REVIEW_OPEN}\n{previous_review}\n{REVIEW_CLOSE}\n\n</details>\n"
    )


def render_meta_header(
    status_line: str,
    stats: ReviewStats,
    provider: str = "Claude Code",
    model: str = "opus",
    job_url: str | None = None,
) -> str:
    """Assembles the sticky marker, hidden stats marker, and top-line status."""
    label = f"{provider} · {model}" if provider != "none" else ""
    job_suffix = (
        f' · <a href="{job_url}" target="_blank" rel="noopener noreferrer">[job]</a>'
        if job_url
        else ""
    )
    meta_line = f"{status_line}"
    if label:
        meta_line += f" · {label}"
    if job_suffix:
        meta_line += job_suffix

    stats_marker = (
        f"<!-- review-stats runs={stats.runs} tokens={stats.tokens} cost={stats.cost:.6f} "
        f"tokens_available={'true' if stats.tokens_available else 'false'} "
        f"cost_available={'true' if stats.cost_available else 'false'} -->"
    )
    return f"{STICKY_MARKER}\n{stats_marker}\n{meta_line}"


def build_start_comment(
    sha: str,
    stats: ReviewStats,
    previous_review: str = "",
    server_url: str = "https://github.com",
    repo: str = "",
    job_url: str | None = None,
) -> str:
    """Builds the in-progress review comment."""
    sha_short = sha[:7]
    sha_link = f'<a href="{server_url}/{repo}/commit/{sha}" target="_blank" rel="noopener noreferrer">#{sha_short}</a>'
    status = f"{SPINNER} <em>Reviewing {sha_link}</em>"
    if stats.runs > 0:
        tokens_str = format_tokens(stats.tokens, available=stats.tokens_available)
        cost_str = format_cost(stats.cost, available=stats.cost_available)
        status += f" · {stats.runs} runs · Σ {tokens_str} · {cost_str}"

    header = render_meta_header(status, stats, job_url=job_url)
    collapsed = build_collapsed_previous(previous_review)
    return f"{header}\n{collapsed}".strip() + "\n"


def build_gate_notice(gate: str, override_reason: str = "") -> str:
    """Generates the footer notice describing gate status or override."""
    if gate == "block":
        return (
            "\n> 🚫 <strong>This review is blocking.</strong> Resolve the findings and push, "
            "or add a <code>NO_LGTM=&lt;reason&gt;</code> line to the PR description to override the gate (the review keeps running).\n"
        )
    if gate == "override":
        escaped_reason = html.escape(override_reason)
        return (
            f"\n> ✅ <strong>Review gate overridden</strong> via <code>NO_LGTM</code> in the PR description "
            f"— merge unblocked despite the verdict. Reason: {escaped_reason}\n"
        )
    return ""


def determine_gate(verdict: str | None, override_active: bool) -> str:
    """Computes merge gate status: pass, override, or block."""
    if verdict == "LGTM":
        return "pass"
    if override_active:
        return "override"
    return "block"


def build_finish_comment(
    sha: str,
    outcome: str,
    review_content: str | dict[str, Any] | None,
    prev_stats: ReviewStats,
    *,
    this_tokens: int = 0,
    this_duration_ms: int = 0,
    this_cost: float = 0.0,
    provider: str = "Claude Code",
    model: str = "opus",
    override_active: bool = False,
    override_reason: str = "",
    previous_review: str = "",
    error_message: str = "",
    server_url: str = "https://github.com",
    repo: str = "",
    job_url: str | None = None,
    summary_url: str | None = None,
) -> tuple[str, str]:
    """Builds the final review comment and returns (comment_body, gate_status)."""
    sha_short = sha[:7]
    sha_link = f'<a href="{server_url}/{repo}/commit/{sha}" target="_blank" rel="noopener noreferrer">#{sha_short}</a>'
    now_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")

    # Accumulate metrics
    new_stats = ReviewStats(
        runs=prev_stats.runs + 1 if provider != "none" else prev_stats.runs,
        tokens=prev_stats.tokens + this_tokens,
        cost=prev_stats.cost + this_cost,
        tokens_available=prev_stats.tokens_available,
        cost_available=prev_stats.cost_available,
    )

    verdict: str | None = None
    rendered_body = ""
    if outcome == "success" and review_content:
        if isinstance(review_content, str):
            try:
                parsed = parse_review(review_content)
                verdict = effective_verdict(parsed)
                rendered_body = render_report(parsed, small_path=True)
            except Exception:
                rendered_body = review_content.strip()
        else:
            parsed = parse_review(review_content)
            verdict = effective_verdict(parsed)
            rendered_body = render_report(parsed, small_path=True)

    gate = determine_gate(verdict, override_active)

    if outcome == "success" and rendered_body:
        status = f"<em>Reviewed {sha_link} at {now_utc}</em>"
    else:
        err_suffix = f" — {html.escape(error_message)}" if error_message else ""
        status = f"⚠️ <em>Review of {sha_link} failed{err_suffix}</em>"

    token_link = (
        f'<a href="{summary_url}" target="_blank" rel="noopener noreferrer">{format_tokens(this_tokens)}</a>'
        if summary_url
        else format_tokens(this_tokens)
    )
    run_stats = (
        f"⏱ {format_duration(this_duration_ms)} · 🪙 {token_link} · {format_cost(this_cost)} "
        f"({new_stats.runs} runs · Σ {format_tokens(new_stats.tokens)} · {format_cost(new_stats.cost)})"
    )
    status += f" · {run_stats}"

    header = render_meta_header(status, new_stats, provider=provider, model=model, job_url=job_url)

    parts = [header]
    if outcome == "success" and rendered_body:
        parts.append(f"\n{REVIEW_OPEN}\n{rendered_body}\n{REVIEW_CLOSE}")
    elif previous_review:
        parts.append(build_collapsed_previous(previous_review))

    notice = build_gate_notice(gate, override_reason)
    if notice:
        parts.append(notice)

    return "\n\n".join(parts).strip() + "\n", gate


def _github_request(
    url: str,
    token: str,
    method: str = "GET",
    data: dict[str, object] | None = None,
) -> object:
    """Performs an authenticated GitHub REST API request using standard urllib."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Code-Review-Agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    encoded_data = json.dumps(data).encode("utf-8") if data is not None else None
    if encoded_data:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content) if content else {}


def find_sticky_comment(repo: str, pr: int, token: str) -> tuple[int | None, str]:
    """Finds existing sticky comment by STICKY_MARKER, returning (comment_id, body)."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr}/comments?per_page=100"
    comments = _github_request(url, token)
    if isinstance(comments, list):
        for c in comments:
            if isinstance(c, dict):
                body = str(c.get("body", ""))
                if STICKY_MARKER in body:
                    raw_id = c.get("id")
                    comment_id = (
                        int(raw_id)
                        if isinstance(raw_id, (int, str)) and str(raw_id).isdigit()
                        else None
                    )
                    return comment_id, body
    return None, ""


def upsert_comment(repo: str, pr: int, comment_id: int | None, body: str, token: str) -> int:
    """Creates or updates the sticky comment on GitHub."""
    if comment_id:
        url = f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}"
        resp = _github_request(url, token, method="PATCH", data={"body": body})
        if isinstance(resp, dict):
            raw_id = resp.get("id")
            if isinstance(raw_id, (int, str)) and str(raw_id).isdigit():
                return int(raw_id)
        return 0
    url = f"https://api.github.com/repos/{repo}/issues/{pr}/comments"
    resp = _github_request(url, token, method="POST", data={"body": body})
    if isinstance(resp, dict):
        raw_id = resp.get("id")
        if isinstance(raw_id, (int, str)) and str(raw_id).isdigit():
            return int(raw_id)
    return 0


def _run_cmd_check_override(args: argparse.Namespace) -> int:
    pr_body = args.body
    if not pr_body and args.event_path and Path(args.event_path).exists():
        with Path(args.event_path).open(encoding="utf-8") as f:
            evt = json.load(f)
            pr_body = evt.get("pull_request", {}).get("body", "")
    elif not pr_body and args.repo and args.pr and args.token:
        url = f"https://api.github.com/repos/{args.repo}/pulls/{args.pr}"
        pull_data = _github_request(url, args.token)
        if isinstance(pull_data, dict):
            pr_body = str(pull_data.get("body", ""))

    active, reason = parse_override_from_body(pr_body)
    set_github_output("active", "true" if active else "false")
    set_github_output("reason", reason)
    print(f"Override: active={active}, reason={reason}")
    return 0


def _run_cmd_start(args: argparse.Namespace) -> int:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    pr = args.pr or int(os.environ.get("PR", "0"))
    sha = args.sha or os.environ.get("SHA", os.environ.get("GITHUB_SHA", ""))
    token = args.token or os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    server_url = args.server_url or os.environ.get("GITHUB_SERVER_URL", "https://github.com")

    if not repo or not pr or not sha:
        print("Missing required parameters for start: repo, pr, sha", file=sys.stderr)
        return 1

    existing_id: int | None = None
    existing_body = ""
    if token:
        existing_id, existing_body = find_sticky_comment(repo, pr, token)

    previous_review = extract_previous_review(existing_body)
    prev_stats = parse_stats_marker(existing_body)

    body = build_start_comment(
        sha=sha,
        stats=prev_stats,
        previous_review=previous_review,
        server_url=server_url,
        repo=repo,
        job_url=args.job_url,
    )

    if token:
        created_id = upsert_comment(repo, pr, existing_id, body, token)
        set_github_output("id", str(created_id))
        set_github_output("comment_id", str(created_id))
        print(f"Started sticky comment: {created_id}")
    else:
        print(body)
    return 0


def _run_cmd_finish(args: argparse.Namespace) -> int:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    pr = args.pr or int(os.environ.get("PR", "0"))
    sha = args.sha or os.environ.get("SHA", os.environ.get("GITHUB_SHA", ""))
    token = args.token or os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    server_url = args.server_url or os.environ.get("GITHUB_SERVER_URL", "https://github.com")

    comment_id = args.comment_id or (
        int(os.environ["COMMENT_ID"]) if "COMMENT_ID" in os.environ else None
    )
    existing_body = ""
    if token and not comment_id:
        comment_id, existing_body = find_sticky_comment(repo, pr, token)
    elif token and comment_id:
        try:
            url = f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}"
            c = _github_request(url, token)
            if isinstance(c, dict):
                existing_body = str(c.get("body", ""))
        except Exception:
            pass

    previous_review = extract_previous_review(existing_body)
    prev_stats = parse_stats_marker(existing_body)

    review_content = ""
    if args.review_file and Path(args.review_file).exists():
        review_content = Path(args.review_file).read_text(encoding="utf-8")

    err_msg = args.error_message
    if not err_msg and args.err_file and Path(args.err_file).exists():
        err_msg = Path(args.err_file).read_text(encoding="utf-8")

    meta: dict[str, Any] = {}
    meta_path = getattr(args, "meta_file", None)
    if not meta_path and args.review_file:
        candidate = Path(args.review_file).parent / "review-meta.json"
        if candidate.exists():
            meta_path = str(candidate)
    if meta_path and Path(meta_path).exists():
        with contextlib.suppress(Exception):
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))

    this_tokens = args.tokens or int(meta.get("tokens", 0))
    this_duration_ms = args.duration_ms or int(meta.get("duration_ms", 0))
    this_cost = args.cost or float(meta.get("cost", 0.0))
    provider = (
        args.provider if args.provider != "Claude Code" else meta.get("provider", "Claude Code")
    )
    model = args.model if args.model != "opus" else meta.get("model", "opus")

    override_active = args.override_active or (
        os.environ.get("OVERRIDE_ACTIVE", "").lower() in {"true", "1", "yes"}
    )
    override_reason = args.override_reason or os.environ.get("OVERRIDE_REASON", "")

    body, gate = build_finish_comment(
        sha=sha,
        outcome=args.outcome,
        review_content=review_content,
        prev_stats=prev_stats,
        this_tokens=this_tokens,
        this_duration_ms=this_duration_ms,
        this_cost=this_cost,
        provider=provider,
        model=model,
        override_active=override_active,
        override_reason=override_reason,
        previous_review=previous_review,
        error_message=err_msg,
        server_url=server_url,
        repo=repo,
        job_url=args.job_url,
    )

    if token and pr:
        upsert_comment(repo, pr, comment_id, body, token)
    set_github_output("gate", gate)
    print(f"Finished sticky comment. Gate: {gate}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for managing GitHub sticky comments without bash."""
    parser = argparse.ArgumentParser(prog="review-comment")
    sub = parser.add_subparsers(dest="command", required=True)

    # check-override
    p_ov = sub.add_parser("check-override", help="Detect NO_LGTM override in PR description")
    p_ov.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    p_ov.add_argument("--pr", type=int, default=None)
    p_ov.add_argument("--token", default=os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN")))
    p_ov.add_argument("--body", default=os.environ.get("PR_BODY"))
    p_ov.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))

    # start
    p_st = sub.add_parser("start", help="Post or update the in-progress sticky review comment")
    p_st.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    p_st.add_argument("--pr", type=int, default=None)
    p_st.add_argument("--sha", default=os.environ.get("SHA", os.environ.get("GITHUB_SHA")))
    p_st.add_argument("--token", default=os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN")))
    p_st.add_argument(
        "--server-url", default=os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    )
    p_st.add_argument("--job-url", default=os.environ.get("JOB_URL"))

    # finish
    p_fn = sub.add_parser(
        "finish", help="Post or update the completed review and determine merge gate"
    )
    p_fn.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    p_fn.add_argument("--pr", type=int, default=None)
    p_fn.add_argument("--sha", default=os.environ.get("SHA", os.environ.get("GITHUB_SHA")))
    p_fn.add_argument("--token", default=os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN")))
    p_fn.add_argument("--comment-id", type=int, default=None)
    p_fn.add_argument("--outcome", default="success", choices=["success", "failure"])
    p_fn.add_argument("--review-file", default=None)
    p_fn.add_argument("--meta-file", default=None)
    p_fn.add_argument("--err-file", default=None)
    p_fn.add_argument("--error-message", default="")
    p_fn.add_argument("--override-active", action="store_true", default=False)
    p_fn.add_argument("--override-reason", default="")
    p_fn.add_argument("--tokens", type=int, default=0)
    p_fn.add_argument("--duration-ms", type=int, default=0)
    p_fn.add_argument("--cost", type=float, default=0.0)
    p_fn.add_argument("--provider", default="Claude Code")
    p_fn.add_argument("--model", default="opus")
    p_fn.add_argument(
        "--server-url", default=os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    )
    p_fn.add_argument("--job-url", default=os.environ.get("JOB_URL"))

    args = parser.parse_args(argv)
    if args.command == "check-override":
        return _run_cmd_check_override(args)
    if args.command == "start":
        return _run_cmd_start(args)
    if args.command == "finish":
        return _run_cmd_finish(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
