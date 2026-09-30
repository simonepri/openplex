"""Evaluates review scoring matrices, test results, and performance artifacts against configured quality rubrics."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from src.infra.tools.review.context.rules_index import ReportContract


@dataclasses.dataclass(frozen=True)
class RenderedMatrix:
    """A fully populated or missing report matrix."""

    title: str
    is_missing: bool
    markdown_table: str
    artifact_path: str | None
    notes: str | None


def _format_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Formats headers and rows into GitHub Flavored Markdown table."""
    if not headers:
        return ""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            if idx < len(col_widths):
                col_widths[idx] = max(col_widths[idx], len(str(cell)))

    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
    sep_line = "| " + " | ".join("-" * col_widths[i] for i in range(len(headers))) + " |"
    row_lines = [
        "| " + " | ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)) + " |"
        for row in rows
    ]
    return "\n".join([header_line, sep_line, *row_lines])


def _ingest_artifact(artifact_file: Path, artifact: str, columns: list[str]) -> str:
    """Ingests artifact file content and formats into markdown."""
    if not artifact.endswith(".json"):
        return artifact_file.read_text(encoding="utf-8")

    data = json.loads(artifact_file.read_text(encoding="utf-8"))
    rows: list[list[str]] = []
    headers = columns or ["Metric", "Baseline", "Candidate", "Delta", "Status"]
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                rows.append([str(item.get(h.lower(), item.get(h, ""))) for h in headers])
    elif isinstance(data, dict):
        for k, v in data.items():
            rows.append([k, str(v)])
            if len(headers) < 2:
                headers = ["Metric", "Value"]
    return _format_markdown_table(headers, rows)


def evaluate_report_contracts(
    repo_root: Path,
    contracts: list[ReportContract],
) -> list[RenderedMatrix]:
    """Evaluates all required report contracts against artifacts and test results."""
    rendered: list[RenderedMatrix] = []

    for c in contracts:
        artifact = c.artifact_pattern
        if artifact:
            artifact_file = repo_root / artifact
            if not artifact_file.exists():
                rendered.append(
                    RenderedMatrix(
                        title=c.title,
                        is_missing=True,
                        markdown_table="",
                        artifact_path=artifact,
                        notes=(
                            f"Required report artifact `{artifact}` was not found. "
                            "Please run the designated benchmark or test script and "
                            "commit the report artifact."
                        ),
                    )
                )
                continue

            # Ingest artifact
            try:
                table_md = _ingest_artifact(artifact_file, artifact, c.columns)
                rendered.append(
                    RenderedMatrix(
                        title=c.title,
                        is_missing=False,
                        markdown_table=table_md,
                        artifact_path=artifact,
                        notes=None,
                    )
                )
            except Exception as e:
                rendered.append(
                    RenderedMatrix(
                        title=c.title,
                        is_missing=True,
                        markdown_table="",
                        artifact_path=artifact,
                        notes=f"Error reading artifact `{artifact}`: {e}",
                    )
                )
        # Table template without specified artifact; render template structure
        elif c.columns:
            rendered.append(
                RenderedMatrix(
                    title=c.title,
                    is_missing=False,
                    markdown_table=_format_markdown_table(c.columns, []),
                    artifact_path=None,
                    notes="Template specified by rule; awaiting test execution metrics.",
                )
            )

    return rendered
