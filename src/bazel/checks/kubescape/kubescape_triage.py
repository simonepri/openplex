#!/usr/bin/env python3
"""Triage Kubescape security scan findings against repository compliance rules and exception policies."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MIN_ARGV_COUNT = 4


def load_json(path_str: str) -> dict[str, Any]:
    with Path(path_str).open("r", encoding="utf-8") as f:
        return json.load(f)


def _collect_failed_control_ids(c: dict[str, Any], failed_controls: set[str]) -> None:
    for sub in c.get("rules", []):
        if sub.get("status") == "failed":
            cid = c.get("controlID")
            if cid:
                failed_controls.add(str(cid))


def _extract_negative_failed_controls(neg_data: dict[str, Any]) -> set[str]:
    neg_failed_controls: set[str] = set()
    for r in neg_data.get("results", []):
        for c in r.get("controls", []):
            _collect_failed_control_ids(c, neg_failed_controls)
    return neg_failed_controls


def _extract_control_rules(
    c: dict[str, Any],
    kind: str,
    name: str,
    ns: str,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    cid = str(c.get("controlID", ""))
    cname = str(c.get("name", ""))
    for sub in c.get("rules", []):
        if sub.get("status") == "failed":
            findings.append({
                "controlID": cid,
                "controlName": cname,
                "kind": kind,
                "name": name,
                "namespace": ns,
                "rule": str(sub.get("name", "")),
            })
    return findings


def _extract_primary_findings(
    primary_data: dict[str, Any],
    primary_resources: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    primary_findings: list[dict[str, str]] = []
    for item in primary_data.get("results", []):
        rid = item.get("resourceID")
        res_obj = primary_resources.get(rid, {})
        kind = str(res_obj.get("kind", ""))
        meta = res_obj.get("metadata", {})
        name = str(meta.get("name") or res_obj.get("name") or "")
        ns = str(meta.get("namespace") or "default")

        for c in item.get("controls", []):
            findings = _extract_control_rules(c, kind, name, ns)
            primary_findings.extend(findings)
    return primary_findings


def _match_exception(f: dict[str, str], exc: dict[str, str]) -> bool:
    if (
        exc.get("controlID") != f["controlID"]
        or exc.get("kind") != f["kind"]
        or exc.get("namespace") != f["namespace"]
    ):
        return False
    prefix = exc.get("namePrefix")
    if prefix is not None:
        return f["name"].startswith(prefix)
    return exc.get("name") == f["name"]


def _find_matching_exception(
    f: dict[str, str],
    reviewed_exceptions: list[dict[str, str]],
) -> int | None:
    for idx, exc in enumerate(reviewed_exceptions):
        if _match_exception(f, exc):
            return idx
    return None


def _audit_stale_exceptions(
    reviewed_exceptions: list[dict[str, str]],
    matched_exceptions: set[int],
    denials: list[str],
) -> None:
    for idx, exc in enumerate(reviewed_exceptions):
        if idx not in matched_exceptions:
            target_name = exc.get("name") or (exc.get("namePrefix", "") + "*")
            denials.append(
                f"stale reviewed exception: control={exc.get('controlID')} kind={exc.get('kind')} name={target_name} namespace={exc.get('namespace')} never observed"
            )


def main() -> int:
    if len(sys.argv) < MIN_ARGV_COUNT:
        return 2

    policy = load_json(sys.argv[1])
    neg_data = load_json(sys.argv[2])
    primary_data = load_json(sys.argv[3])

    if not _extract_negative_failed_controls(neg_data):
        return 1

    reviewed_exceptions = policy.get("reviewedExceptions", [])
    primary_resources = {
        r.get("resourceID"): r.get("object", {}) for r in primary_data.get("resources", [])
    }
    primary_findings = _extract_primary_findings(primary_data, primary_resources)

    matched_exceptions: set[int] = set()
    denials: list[str] = []

    for f in primary_findings:
        matched_idx = _find_matching_exception(f, reviewed_exceptions)
        if matched_idx is not None:
            matched_exceptions.add(matched_idx)
        else:
            denials.append(
                f"untriaged finding: control={f['controlID']} ({f['controlName']}) kind={f['kind']} name={f['name']} namespace={f['namespace']} rule={f['rule']}"
            )

    _audit_stale_exceptions(reviewed_exceptions, matched_exceptions, denials)

    if denials:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
