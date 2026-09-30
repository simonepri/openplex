"""Compiles diff analysis, impacted target graphs, and RuleSync rubrics into structured prompt bundles for LLMs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.infra.tools.review.engine.report import compute_size_bucket

if TYPE_CHECKING:
    from src.infra.tools.review.context.bazel_diff import BazelDiffResult
    from src.infra.tools.review.context.codegraph import CodeGraphResult
    from src.infra.tools.review.context.git_diff import FileDiff, GitDiffResult
    from src.infra.tools.review.context.rules_index import RuleDiffResult
    from src.infra.tools.review.engine.matrix import RenderedMatrix
    from src.infra.tools.review.engine.rubrics import DomainRubric


def _render_changed_files(files: list[FileDiff]) -> str:
    lines = [
        "## 1. Changed Files",
        "| Status | Path | + / - |",
        "| :---: | :--- | :---: |",
    ]
    for f in files:
        old_info = f" (from `{f.old_path}`)" if f.old_path else ""
        lines.append(f"| `{f.status}` | `{f.path}`{old_info} | +{f.additions} / -{f.deletions} |")
    return "\n".join(lines)


def _render_bazel_impact(bazel_diff: BazelDiffResult) -> str:
    lines = ["## 2. Bazel Dependency Graph Impact"]
    if bazel_diff.direct_targets:
        lines.append(
            f"**Direct Targets ({len(bazel_diff.direct_targets)})**:\n"
            + "\n".join(f"- `{t}`" for t in bazel_diff.direct_targets[:15])
        )
    if bazel_diff.affected_tests:
        lines.append(
            f"\n**Affected Tests to Execute ({len(bazel_diff.affected_tests)})**:\n"
            + "\n".join(f"- `{t}`" for t in bazel_diff.affected_tests[:15])
        )
    if not bazel_diff.direct_targets and not bazel_diff.affected_tests:
        lines.append("*No direct Bazel code targets impacted.*")
    return "\n".join(lines)


def _render_codegraph_impact(codegraph: CodeGraphResult) -> str:
    lines = ["## 3. CodeGraph Symbol & Caller Impact"]
    if codegraph.impacted_symbols:
        lines.append("**Impacted Downstream Symbols**:")
        for sym in codegraph.impacted_symbols[:15]:
            lines.append(f"- `{sym.symbol}` ({sym.kind}) in `{sym.file_path}:{sym.line}`")
    if codegraph.context_markdown:
        lines.append(
            "\n**Structural Graph Context**:\n```\n" + codegraph.context_markdown.strip() + "\n```"
        )
    if not codegraph.impacted_symbols and not codegraph.context_markdown:
        lines.append("*No symbol impact detected.*")
    return "\n".join(lines)


def _render_rule_diff(rule_diff: RuleDiffResult | None) -> str | None:
    if not rule_diff or not (
        rule_diff.added_rules or rule_diff.removed_rules or rule_diff.modified_rules
    ):
        return None
    lines = ["## 4. Rule Changes in this Diff"]
    if rule_diff.added_rules:
        lines.append(
            f"**Added Rules ({len(rule_diff.added_rules)})**:\n"
            + "\n".join(f"- **{r.title}** ({r.domain})" for r in rule_diff.added_rules)
        )
    if rule_diff.removed_rules:
        lines.append(
            f"**Removed Rules ({len(rule_diff.removed_rules)})**:\n"
            + "\n".join(f"- **{r.title}** ({r.domain})" for r in rule_diff.removed_rules)
        )
    if rule_diff.modified_rules:
        lines.append(
            f"**Modified Rules ({len(rule_diff.modified_rules)})**:\n"
            + "\n".join(f"- **{old.title}** ({old.domain})" for old, _ in rule_diff.modified_rules)
        )
    return "\n".join(lines)


def _render_matrices(matrices: list[RenderedMatrix] | None) -> str | None:
    if not matrices:
        return None
    lines = ["## 5. Verification Matrices & Report Artifacts"]
    for m in matrices:
        if m.is_missing:
            lines.append(f"### ⚠️ Missing Required Report: {m.title}\n{m.notes}\n")
        else:
            lines.append(f"### 📊 {m.title}\n{m.markdown_table}\n")
            if m.notes:
                lines.append(f"*{m.notes}*\n")
    return "\n".join(lines)


def _render_rubrics(rubrics: list[DomainRubric]) -> str:
    lines = [
        "## 6. Applicable Review Rubrics",
        "Evaluate the code changes against the following applicable rules derived directly from repository guidance. "
        "For each rule, verify if the diff adheres to or violates the requirement.",
    ]
    for rub in rubrics:
        lines.append(f"### Domain: {rub.domain.capitalize()}")
        for crit in rub.criteria:
            lines.append(
                f"- [ ] **{crit.title}** (`{crit.source_path}:{crit.line_number}`): {crit.requirement}"
            )
    return "\n".join(lines)


def _render_instructions(diff: GitDiffResult) -> str:
    size_bucket = compute_size_bucket(len(diff.files), diff.total_additions + diff.total_deletions)
    return (
        "## Review Instructions\n\n"
        "You are performing a code review strictly following the repository's review format and contract.\n\n"
        "### Gating Verdict\n"
        "Emit your verdict on the very first line:\n"
        f"🏷️ **Verdict**: `VERDICT [{size_bucket}]` — 1-2 sentence overall status\n\n"
        "Where `VERDICT` is:\n"
        "- `DO NOT MERGE` if there are any 🔴 **Blocker** findings.\n"
        "- `CHANGES REQUESTED` if there are any 🟡 **Improvement** findings (and no Blockers).\n"
        "- `LGTM` if there are no Blockers and no Improvements (🔵 Nit, 🟣 Question, and ⚪ Existing do not block).\n\n"
        "### Optional Split Suggestion\n"
        "If the change is large or mixes disparate concerns, suggest how to split it:\n"
        "✂️ **Split suggestion**:\n"
        "- PR 1: ...\n"
        "- PR 2: ...\n"
        "(Omit this entire section if the change is cohesive).\n\n"
        "### Findings Report\n"
        "If there are any findings, output them under `📋 **Report**:` ordered strictly by severity:\n"
        "- 🔴 **Blocker**: Defect making the code incorrect, unsafe, or causing regressions.\n"
        "- 🟡 **Improvement**: Concrete enhancement to otherwise correct code with a specific consequence.\n"
        "- 🔵 **Nit**: Minor polish, typo, or style preference.\n"
        "- 🟣 **Question**: Clarification on suspicious code or design choice.\n"
        "- ⚪ **Existing**: Pre-existing tech debt not introduced by this diff.\n\n"
        "Each finding MUST be a single bullet formatted as:\n"
        "- <small>`path/to/file:line`</small> — Description of issue and concrete fix.\n"
        "(Use the `<small>` tags around the backticked location so the file path renders in a smaller font).\n\n"
        "Omit empty severity sections completely. If there are no findings, omit the `📋 **Report**:` section entirely."
    )


def compile_review_prompt(
    diff: GitDiffResult,
    bazel_diff: BazelDiffResult,
    codegraph: CodeGraphResult,
    rubrics: list[DomainRubric],
    rule_diff: RuleDiffResult | None = None,
    matrices: list[RenderedMatrix] | None = None,
) -> str:
    """Compiles the complete review prompt bundle."""
    sections = [
        "# Pull Request Review Bundle\n"
        f"**Comparison Range**: `{diff.base_ref}` ... `{diff.target_ref}`\n"
        f"**Stats**: {len(diff.files)} files changed, +{diff.total_additions} / -{diff.total_deletions} lines",
        _render_changed_files(diff.files),
        _render_bazel_impact(bazel_diff),
        _render_codegraph_impact(codegraph),
    ]

    r_diff = _render_rule_diff(rule_diff)
    if r_diff:
        sections.append(r_diff)

    m_section = _render_matrices(matrices)
    if m_section:
        sections.append(m_section)

    sections.extend([
        _render_rubrics(rubrics),
        "## 7. Unified Git Diff\n```diff\n" + diff.raw_patch.strip() + "\n```",
        _render_instructions(diff),
    ])

    return "\n\n".join(sections)
