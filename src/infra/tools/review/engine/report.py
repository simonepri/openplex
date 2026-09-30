"""Formats automated code review findings into GitHub Markdown summaries and structured JSON reports."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

SECTIONS = [
    ("blocker", "🔴 **Blocker**"),
    ("improvement", "🟡 **Improvement**"),
    ("nit", "🔵 **Nit**"),
    ("question", "🟣 **Question**"),
    ("existing", "⚪ **Existing**"),
]
SEVERITIES = [key for key, _ in SECTIONS]
VERDICTS = ["LGTM", "CHANGES REQUESTED", "DO NOT MERGE"]
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]


@dataclasses.dataclass(frozen=True)
class ReviewFinding:
    """A specific issue or required action found during review."""

    severity: str  # 'blocker', 'improvement', 'nit', 'question', 'existing'
    location: str
    body: str
    rule_id: str = ""
    rule_title: str = ""


@dataclasses.dataclass(frozen=True)
class ReviewReport:
    """Final consolidated review report."""

    verdict: str  # 'LGTM', 'CHANGES REQUESTED', 'DO NOT MERGE'
    size: str  # 'XS', 'S', 'M', 'L', 'XL', 'XXL'
    summary: str
    split: list[str] = dataclasses.field(default_factory=list)
    findings: list[ReviewFinding] = dataclasses.field(default_factory=list)
    raw_markdown: str = ""


def compute_size_bucket(files: int, sloc: int) -> str:
    """Derives size bucket (XS..XXL) as max of line churn and file count."""
    if sloc < 50:
        lr = 0
    elif sloc < 200:
        lr = 1
    elif sloc < 400:
        lr = 2
    elif sloc < 1000:
        lr = 3
    elif sloc < 2000:
        lr = 4
    else:
        lr = 5

    if files <= 2:
        fr = 0
    elif files <= 5:
        fr = 1
    elif files <= 10:
        fr = 2
    elif files <= 20:
        fr = 3
    elif files <= 49:
        fr = 4
    else:
        fr = 5

    return SIZES[max(lr, fr)]


def review_schema() -> dict[str, Any]:
    """The JSON Schema handed to claude --json-schema."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "size", "summary", "split", "findings"],
        "properties": {
            "verdict": {"type": "string", "enum": VERDICTS},
            "size": {"type": "string", "enum": SIZES},
            "summary": {"type": "string", "minLength": 1},
            "split": {"type": "array", "items": {"type": "string"}},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["severity", "location", "body"],
                    "properties": {
                        "severity": {"type": "string", "enum": SEVERITIES},
                        "location": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
            },
        },
    }


def sanitize_summary(summary: str) -> str:
    """Coerce the summary into a single plain-text line for the verdict header."""
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", summary.strip())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\s+", " ", text).strip()
    return text or summary.strip()


def _validate_findings(findings: list[Any]) -> None:
    for f in findings:
        if not isinstance(f, dict) or f.get("severity") not in SEVERITIES:
            raise ValueError(f"each finding needs a severity in {SEVERITIES}")
        if not isinstance(f.get("body"), str) or not f["body"].strip():
            raise ValueError("each finding needs a non-empty body")
        if not isinstance(f.get("location", ""), str):
            raise ValueError("each finding location must be a string")


def parse_review(data: str | dict[str, Any] | Path) -> dict[str, Any]:
    """Load and validate the structured-output object, raising ValueError on any contract violation."""
    if isinstance(data, Path):
        data = json.loads(data.read_text(encoding="utf-8"))
    elif isinstance(data, str):
        data = json.loads(data)

    if not isinstance(data, dict):
        raise ValueError("payload is not an object")

    required = {"verdict", "size", "summary", "split", "findings"}
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError("missing required top-level fields: " + ", ".join(missing))

    extra = sorted(data.keys() - required)
    if extra:
        raise ValueError("unexpected top-level fields: " + ", ".join(extra))

    if data.get("verdict") not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    if data.get("size") not in SIZES:
        raise ValueError(f"size must be one of {SIZES}")

    summary = data["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-empty string")
    data["summary"] = sanitize_summary(summary)

    split = data["split"]
    if not isinstance(split, list) or not all(isinstance(s, str) for s in split):
        raise ValueError("split must be a list of strings")

    findings = data["findings"]
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")

    _validate_findings(findings)

    return data


def effective_verdict(review: dict[str, Any]) -> str:
    """The gating verdict: the more severe of the reported verdict and what the findings imply."""
    severities = {f["severity"] for f in review.get("findings", [])}
    implied = (
        "DO NOT MERGE"
        if "blocker" in severities
        else "CHANGES REQUESTED"
        if "improvement" in severities
        else "LGTM"
    )
    return max(review["verdict"], implied, key=VERDICTS.index)


def render_finding(finding: dict[str, Any], *, small_path: bool = True) -> str:
    """Renders a single finding bullet. When small_path is True, wraps path in <small>."""
    body = " ".join(finding["body"].split("\n")).strip()
    location = finding.get("location", "").strip()
    if not location:
        return f"- {body}"
    if small_path:
        return f"- <small>`{location}`</small> — {body}"
    return f"- `{location}` — {body}"


def render_section(
    review: dict[str, Any], severity: str, heading: str, *, small_path: bool = True
) -> str:
    """One severity section (heading + bullets), or empty string if no findings for that severity."""
    bullets = [
        render_finding(f, small_path=small_path)
        for f in review.get("findings", [])
        if f["severity"] == severity
    ]
    if not bullets:
        return ""
    return heading + "\n" + "\n".join(bullets)


def render_report(review: dict[str, Any], *, small_path: bool = True) -> str:
    """Renders full review markdown with verdict line, optional split, and report sections."""
    verdict = effective_verdict(review)
    size = review["size"]
    summary = review["summary"].strip()
    verdict_line = f"🏷️ **Verdict**: `{verdict} [{size}]` — {summary}"
    blocks = [verdict_line]

    split = [s.strip() for s in review.get("split", []) if s.strip()]
    if split:
        blocks.append("✂️ **Split suggestion**:\n" + "\n".join(f"- {s}" for s in split))

    sections = [
        section
        for section in (
            render_section(review, key, head, small_path=small_path) for key, head in SECTIONS
        )
        if section
    ]
    if sections:
        blocks.append("📋 **Report**:\n\n" + "\n\n".join(sections))

    return "\n\n".join(blocks) + "\n"


def format_markdown_report(report: ReviewReport, *, small_path: bool = True) -> str:
    """Converts a ReviewReport dataclass into review markdown."""
    data = {
        "verdict": report.verdict,
        "size": report.size,
        "summary": report.summary,
        "split": report.split,
        "findings": [
            {
                "severity": f.severity,
                "location": f.location,
                "body": f.body,
            }
            for f in report.findings
        ],
    }
    return render_report(data, small_path=small_path)


def format_json_report(report: ReviewReport) -> str:
    """Formats ReviewReport dataclass into machine-readable JSON matching the schema."""
    data = {
        "verdict": report.verdict,
        "size": report.size,
        "summary": report.summary,
        "split": report.split,
        "findings": [
            {
                "severity": f.severity,
                "location": f.location,
                "body": f.body,
            }
            for f in report.findings
        ],
    }
    return json.dumps(data, indent=2)
