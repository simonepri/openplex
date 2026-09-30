#!/usr/bin/env python3
"""Filter and triage Trivy misconfiguration findings against ignore rules and security policies."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_ARGV_COUNT = 4
GATED_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})


@dataclass
class PolicyContext:
    false_positives: set[str]
    observed_fp: set[str]
    rule_exceptions: set[str]
    observed_rule_exceptions: set[str]
    ordinary_rules: list[tuple[str, str]]


def _extract_result_findings(
    r: dict[str, Any],
    target: str,
    results: list[dict[str, str]],
) -> None:
    for m in r.get("Misconfigurations", []):
        if m.get("Status") == "FAIL":
            results.append({
                "target": target,
                "id": str(m.get("ID", "")),
                "severity": str(m.get("Severity", "UNKNOWN")).upper(),
            })
    for v in r.get("Vulnerabilities", []):
        results.append({
            "target": target,
            "id": str(v.get("VulnerabilityID", "")),
            "severity": str(v.get("Severity", "UNKNOWN")).upper(),
        })


def extract_findings(
    report: dict[str, Any],
    neg_path: str,
    *,
    want_negative: bool,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for r in report.get("Results", []):
        target = neg_path if want_negative else str(r.get("Target") or ".")
        _extract_result_findings(r, target, results)
    return results


def _is_ordinary_conformance(
    target: str,
    rules: list[tuple[str, str]],
) -> bool:
    return ".." not in Path(target).parts and any(
        target.startswith(p) and target.endswith(s) for p, s in rules
    )


def _audit_finding(f: dict[str, str], ctx: PolicyContext) -> str | None:
    key = f"{f['target']}|{f['id']}"
    if key in ctx.false_positives:
        ctx.observed_fp.add(key)
    elif f["id"] in ctx.rule_exceptions:
        ctx.observed_rule_exceptions.add(f["id"])
    elif _is_ordinary_conformance(f["target"], ctx.ordinary_rules):
        pass
    elif f["severity"] in GATED_SEVERITIES:
        return f"untriaged {f['severity']} finding in deployable source: {f['target']} {f['id']}"
    return None


def _check_stale_policy(ctx: PolicyContext, denials: list[str]) -> None:
    for key in ctx.false_positives - ctx.observed_fp:
        denials.append(f"scanner false-positive policy is stale: {key} never observed")
    for rule_id in ctx.rule_exceptions - ctx.observed_rule_exceptions:
        denials.append(f"scanner rule exception policy is stale: {rule_id} never observed")


def _load_primary_findings(primary_files: list[Path], neg_path: str) -> list[dict[str, str]]:
    primary_findings: list[dict[str, str]] = []
    for pf in primary_files:
        with pf.open(encoding="utf-8") as f:
            primary_findings.extend(extract_findings(json.load(f), neg_path, want_negative=False))
    return primary_findings


def _init_policy_context(policy: dict[str, Any]) -> PolicyContext:
    ordinary_rules = [
        (str(r.get("prefix", "src/infra/definitions/conformance/")), str(r["suffix"]))
        for r in policy.get("ordinaryConformanceFixtureRules", [])
    ]
    return PolicyContext(
        false_positives={f"{e['path']}|{e['id']}" for e in policy.get("scannerFalsePositives", [])},
        observed_fp=set(),
        rule_exceptions={e["id"] for e in policy.get("scannerRuleExceptions", [])},
        observed_rule_exceptions=set(),
        ordinary_rules=ordinary_rules,
    )


def _collect_denials(
    primary_findings: list[dict[str, str]],
    negative_findings: list[dict[str, str]],
    neg_path: str,
    ctx: PolicyContext,
) -> list[str]:
    denials: list[str] = []
    if not negative_findings:
        denials.append("intentional security-negative fixture produced no findings")
    for f in primary_findings:
        if f["target"] == neg_path:
            denials.append("intentional security-negative fixture leaked into primary scan")

    for f in primary_findings + negative_findings:
        if f["target"] != neg_path:
            err = _audit_finding(f, ctx)
            if err:
                denials.append(err)

    _check_stale_policy(ctx, denials)
    return denials


def main() -> int:
    if len(sys.argv) < MIN_ARGV_COUNT:
        return 2

    policy_file = Path(sys.argv[1])
    negative_file = Path(sys.argv[2])
    primary_files = [Path(p) for p in sys.argv[3:]]

    with policy_file.open(encoding="utf-8") as f:
        policy: dict[str, Any] = json.load(f)
    with negative_file.open(encoding="utf-8") as f:
        negative: dict[str, Any] = json.load(f)

    if policy.get("schemaVersion") != 1:
        return 1

    neg_entry = policy["intentionalSecurityNegativeFixtures"][0]
    neg_path = str(neg_entry["path"])

    primary_findings = _load_primary_findings(primary_files, neg_path)
    negative_findings = extract_findings(negative, neg_path, want_negative=True)

    ctx = _init_policy_context(policy)
    denials = _collect_denials(primary_findings, negative_findings, neg_path, ctx)

    if denials:
        for d in denials:
            print(f"TRIVY_TRIAGE_ERROR: {d}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
