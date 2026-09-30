"""Parses RuleSync documentation rules and maps changed files to applicable quality guidelines and check rubrics."""

from __future__ import annotations

import dataclasses
import fnmatch
import re
import subprocess
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class ReportContract:
    """Declared custom report or matrix required by a rule."""

    title: str
    artifact_pattern: str | None
    columns: list[str]
    raw_snippet: str


@dataclasses.dataclass(frozen=True)
class RuleItem:
    """Single rule extracted from a RuleSync file."""

    rule_id: str
    title: str
    description: str
    domain: str
    path: str
    line_number: int
    is_global: bool
    globs: list[str]
    report_contract: ReportContract | None


@dataclasses.dataclass(frozen=True)
class RulesIndexResult:
    """Applicable rules index across changed files."""

    applicable_rules: list[RuleItem]
    rules_by_domain: dict[str, list[RuleItem]]
    file_to_rules: dict[str, list[RuleItem]]
    report_contracts: list[ReportContract]


@dataclasses.dataclass(frozen=True)
class RuleDiffResult:
    """Differences between rule definitions across base and target revisions."""

    added_rules: list[RuleItem]
    removed_rules: list[RuleItem]
    modified_rules: list[tuple[RuleItem, RuleItem]]


def parse_frontmatter(content: str) -> tuple[dict[str, list[str]], str]:
    """Extract frontmatter and body."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not m:
        return {}, content
    fm_text = str(m.group(1))
    body = str(m.group(2))
    globs = []
    in_globs = False
    for line in fm_text.splitlines():
        ls = line.strip()
        if re.match(r"^globs\s*:", ls):
            in_globs = True
            continue
        if in_globs:
            if ls.startswith("-"):
                val = ls.lstrip("-").strip().strip("\"'")
                if val:
                    globs.append(val)
            elif ls and not ls.startswith("#"):
                in_globs = False
    return {"globs": globs}, body


def _parse_table_header(text: str) -> list[str]:
    """Extract column headers from markdown table text."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|"):
            cols = [c.strip() for c in line.strip("|").split("|")]
            if cols and not all(set(c) <= {"-", ":", " "} for c in cols):
                return cols
    return []


def extract_report_contract(title: str, text: str) -> ReportContract | None:
    """Detect if a rule declares a report template, matrix, or report artifact."""
    lower = text.lower()
    has_report = any(
        k in lower for k in ("report template", "report table", "matrix", "benchmark matrix")
    )
    artifact_m = re.search(r"(\.review/reports/[a-zA-Z0-9_\-\.]+\.(?:json|md|yaml))", text)
    artifact = artifact_m.group(1) if artifact_m else None
    columns = _parse_table_header(text)

    if has_report or artifact or columns:
        return ReportContract(
            title=title,
            artifact_pattern=artifact,
            columns=columns,
            raw_snippet=text,
        )
    return None


def parse_rule_file(repo_root: Path, file_path: Path) -> list[RuleItem]:
    """Parses a rulesync markdown file into individual RuleItems."""
    rel = file_path.relative_to(repo_root)
    is_global = len(rel.parts) == 2 and rel.parts[0] == "agents"
    domain = file_path.name
    for ext in (".rulesync.md", ".rules.md", ".rulesync", ".rules", ".md"):
        domain = domain.removesuffix(ext)
    content = file_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(content)
    globs = fm.get("globs", [])

    rules: list[RuleItem] = []
    current_rule: tuple[str, str, int] | None = None
    sublines: list[str] = []

    def commit_current() -> None:
        nonlocal current_rule, sublines
        if not current_rule:
            return
        title, first_desc, lnum = current_rule
        full_desc = "\n".join([first_desc, *sublines]).strip()
        rule_id = f"{domain}_{re.sub(r'[^a-zA-Z0-9]+', '_', title.lower()).strip('_')}"
        contract = extract_report_contract(title, full_desc)
        rules.append(
            RuleItem(
                rule_id=rule_id,
                title=title,
                description=full_desc,
                domain=domain,
                path=str(rel),
                line_number=lnum,
                is_global=is_global,
                globs=globs,
                report_contract=contract,
            )
        )
        current_rule = None
        sublines = []

    for idx, line in enumerate(body.splitlines(), start=1):
        # Match bullet rule: - **<Title>**: <Desc>
        m = re.match(r"^\s*-\s+\*\*([^*]+)\*\*[:\s—]+(.*)$", line)
        if m:
            commit_current()
            title = str(m.group(1)).strip()
            desc = str(m.group(2)).strip()
            current_rule = (title, desc, idx)
        elif current_rule:
            if line.startswith(("## ", "# ")):
                commit_current()
            else:
                sublines.append(line)

    commit_current()
    return rules


def matches_rule(file_path: str, rule: RuleItem) -> bool:
    """Checks whether a relative file path matches a rule's scope and globs."""
    scope_dir = Path(rule.path).parent
    if scope_dir.name == "agents":
        scope_dir = scope_dir.parent

    # Non-global rules only apply within their directory scope
    if not rule.is_global and scope_dir.as_posix() != ".":
        prefix = f"{scope_dir.as_posix()}/"
        if not file_path.startswith(prefix) and file_path != scope_dir.as_posix():
            return False

    if not rule.globs:
        return True

    return any(fnmatch.fnmatch(file_path, g) for g in rule.globs)


def get_applicable_rules(repo_root: Path, changed_files: list[str]) -> RulesIndexResult:
    """Finds all rules applicable to the changed files."""
    all_rule_files = list(repo_root.glob("agents/*.rules.rulesync.md")) + list(
        repo_root.glob("src/**/agents/*.rules.rulesync.md")
    )

    all_rules: list[RuleItem] = []
    for rf in sorted(all_rule_files):
        all_rules.extend(parse_rule_file(repo_root, rf))

    applicable: list[RuleItem] = []
    rules_by_domain: dict[str, list[RuleItem]] = {}
    file_to_rules: dict[str, list[RuleItem]] = {f: [] for f in changed_files}
    contracts: list[ReportContract] = []

    for rule in all_rules:
        matched_any = False
        for f in changed_files:
            if matches_rule(f, rule):
                matched_any = True
                file_to_rules[f].append(rule)

        if matched_any:
            applicable.append(rule)
            rules_by_domain.setdefault(rule.domain, []).append(rule)
            if rule.report_contract:
                contracts.append(rule.report_contract)

    return RulesIndexResult(
        applicable_rules=applicable,
        rules_by_domain=rules_by_domain,
        file_to_rules=file_to_rules,
        report_contracts=contracts,
    )


def diff_rules(repo_root: Path, base_ref: str, target_ref: str) -> RuleDiffResult:
    """Compares rule definitions between base and target git revisions."""

    def get_rules_at_rev(ref: str) -> dict[str, RuleItem]:
        if ref == "WORKTREE":
            files = list(repo_root.glob("agents/*.rules.rulesync.md")) + list(
                repo_root.glob("src/**/agents/*.rules.rulesync.md")
            )
            items = []
            for f in files:
                items.extend(parse_rule_file(repo_root, f))
            return {r.rule_id: r for r in items}

        # Query git files at ref
        cmd = ["git", "ls-tree", "-r", "--name-only", ref]
        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        rule_paths = [
            p
            for p in res.stdout.splitlines()
            if (p.startswith("agents/") or "/agents/" in p) and p.endswith(".rulesync.md")
        ]
        items = []
        for rp in rule_paths:
            show = subprocess.run(
                ["git", "show", f"{ref}:{rp}"], cwd=repo_root, capture_output=True, text=True
            )
            if show.returncode == 0:
                # Mock read by writing to temp or parsing text directly
                domain = Path(rp).stem.replace(".rules", "").replace(".rulesync", "")
                fm, body = parse_frontmatter(show.stdout)
                for idx, line in enumerate(body.splitlines(), start=1):
                    m = re.match(r"^\s*-\s+\*\*([^*]+)\*\*[:\s—]+(.*)$", line)
                    if m:
                        title = m.group(1).strip()
                        desc = m.group(2).strip()
                        r_id = f"{domain}_{re.sub(r'[^a-zA-Z0-9]+', '_', title.lower()).strip('_')}"
                        items.append(
                            RuleItem(
                                rule_id=r_id,
                                title=title,
                                description=desc,
                                domain=domain,
                                path=rp,
                                line_number=idx,
                                is_global=rp.startswith("agents/"),
                                globs=fm.get("globs", []),
                                report_contract=extract_report_contract(title, desc),
                            )
                        )
        return {r.rule_id: r for r in items}

    base_rules = get_rules_at_rev(base_ref)
    target_rules = get_rules_at_rev(target_ref)

    added = [r for r_id, r in target_rules.items() if r_id not in base_rules]
    removed = [r for r_id, r in base_rules.items() if r_id not in target_rules]
    modified = [
        (base_rules[r_id], target_rules[r_id])
        for r_id in target_rules
        if r_id in base_rules and base_rules[r_id].description != target_rules[r_id].description
    ]

    return RuleDiffResult(added_rules=added, removed_rules=removed, modified_rules=modified)
