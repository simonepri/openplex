#!/usr/bin/env python3
"""Enforce reviewed SPDX license allowlists over container base images using Trivy and Syft SBOM scans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# One Trivy report per oci.pull repository, built by Bazel from the pulled
# image; see src/bazel/checks/BUILD.bazel.
REPORTS_TARGET = "//src/bazel/checks:license_image_reports"
REPORTS_DIR = "src/bazel/checks/license_images/reports"


def extract_base_images(module_file: Path) -> dict[str, str]:
    """Map each oci.pull repository name to its digest-pinned image reference."""
    content = module_file.read_text(encoding="utf-8")
    blocks = re.findall(r"oci[.]pull\((.*?)\n\)", content, re.DOTALL)
    images: dict[str, str] = {}
    for block in blocks:
        name = re.findall(r'^\s*name\s*=\s*"([^"]+)"', block, re.MULTILINE)
        img = re.findall(r'^\s*image\s*=\s*"([^"]+)"', block, re.MULTILINE)
        digest = re.findall(r'^\s*digest\s*=\s*"(sha256:[a-f0-9]{64})"', block, re.MULTILINE)
        if len(name) == 1 and len(img) == 1 and len(digest) == 1:
            images[name[0]] = f"{img[0]}@{digest[0]}"
    if not images:
        raise ValueError("MODULE.bazel contains no workload OCI base images")
    return images


def license_tokens(license_str: str) -> list[str]:
    cleaned = re.sub(r"[()]", " ", license_str)
    return [t for t in cleaned.split() if t and t not in {"AND", "OR", "WITH"}]


def normalize_debian_license(
    package: str,
    license_str: str,
    aliases: dict[str, str],
    package_facts: dict[tuple[str, str], str],
) -> str:
    if license_str in aliases:
        return aliases[license_str]
    return package_facts.get((package, license_str), license_str)


@dataclass(frozen=True)
class LicensePolicy:
    allow_everywhere: set[str]
    allow_os: set[str]
    aliases: dict[str, str]
    package_facts: dict[tuple[str, str], str]


@dataclass(frozen=True)
class PackageContext:
    package: str
    license_str: str
    is_os_pkg: bool = False
    is_debian: bool = False


@dataclass(frozen=True)
class AuditConfig:
    policy: LicensePolicy
    allowed_base_families: set[str]
    reviewed_rules: list[dict[str, Any]]


def is_allowed(
    ctx: PackageContext,
    policy: LicensePolicy,
) -> bool:
    effective = (
        normalize_debian_license(ctx.package, ctx.license_str, policy.aliases, policy.package_facts)
        if ctx.is_debian
        else ctx.license_str
    )
    if effective == "debian-adhoc-permissive":
        return True
    tokens = license_tokens(effective)
    if not tokens:
        return False
    return all(
        t in policy.allow_everywhere or (ctx.is_os_pkg and t in policy.allow_os) for t in tokens
    )


def build_reports(root: Path) -> Path | None:
    """Build the image license reports and return the directory that holds them."""
    output_root = os.environ.get("BAZEL_OUTPUT_ROOT") or str(root / ".tmp/state/bazel")
    bazel = ["bazel", f"--output_user_root={output_root}"]
    flags = shlex.split(os.environ.get("BAZEL_CONFIG_FLAGS", ""))
    build = subprocess.run(
        [
            *bazel,
            "build",
            *flags,
            "--remote_download_outputs=toplevel",
            "--noshow_progress",
            "--ui_event_filters=-info",
            REPORTS_TARGET,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        print(f"building {REPORTS_TARGET} failed:\n{build.stderr[-2000:]}", file=sys.stderr)
        return None
    info = subprocess.run(
        [*bazel, "info", *flags, "bazel-bin"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if info.returncode != 0:
        print(f"bazel info bazel-bin failed:\n{info.stderr[-2000:]}", file=sys.stderr)
        return None
    return Path(info.stdout.strip()) / REPORTS_DIR


def load_report(report_dir: Path, repo: str, image: str) -> dict[str, Any] | None:
    """Load the report of one pinned image, checking that it scanned that image."""
    path = report_dir / f"{repo}.json"
    if not path.is_file():
        print(
            f"no license report for oci.pull {repo}; add it to LICENSE_IMAGES "
            "in src/bazel/checks/BUILD.bazel",
            file=sys.stderr,
        )
        return None
    with path.open(encoding="utf-8") as f:
        report = json.load(f)
    if report.get("ArtifactName") != image:
        print(
            f"license report for {repo} names {report.get('ArtifactName')}, not {image}; "
            "fix its image in LICENSE_IMAGES in src/bazel/checks/BUILD.bazel",
            file=sys.stderr,
        )
        return None
    return report


def _collect_packages_items(packages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for pkg in packages:
        pname = pkg.get("Name", "")
        lics = pkg.get("Licenses")
        if isinstance(lics, list) and lics:
            for lic_name in lics:
                items.append((pname, lic_name))
        else:
            items.append((pname, "UNKNOWN"))
    return items


def collect_package_items(r: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract (package_name, license_name) pairs from a Trivy result entry."""
    r_class = r.get("Class")
    if r_class == "license":
        return [(lic.get("PkgName", ""), lic.get("Name", "")) for lic in (r.get("Licenses") or [])]
    if isinstance(r.get("Packages"), list):
        return _collect_packages_items(r.get("Packages") or [])
    return []


def load_exception_inventories(
    root: Path, exceptions_data: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Validate exception hashes and load evidence payloads."""
    inventories: list[dict[str, Any]] = []
    for exc in exceptions_data.get("exceptions", []):
        if "evidence" not in exc or "evidence_sha256" not in exc:
            continue
        ev_path = root / exc["evidence"]
        if not ev_path.is_file():
            return None
        data = ev_path.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != exc["evidence_sha256"]:
            return None
        inventories.append(json.loads(data.decode("utf-8")))
    return inventories


def _find_unreviewed_os(results: list[dict[str, Any]], allowed_base_families: set[str]) -> set[str]:
    unreviewed: set[str] = set()
    for r in results:
        if r.get("Class") == "os-pkgs":
            os_type = r.get("Type")
            if os_type and os_type not in allowed_base_families:
                unreviewed.add(os_type)
    return unreviewed


def audit_image(
    image: str,
    report_data: dict[str, Any],
    exception_inventories: list[dict[str, Any]],
    config: AuditConfig,
) -> tuple[list[dict[str, str]], set[str], int]:
    """Audit single image Trivy scan result against policy and normalization rules."""
    artifact = report_data.get("ArtifactName", image)
    results = report_data.get("Results", []) or []
    has_os = any(r.get("Class") == "os-pkgs" for r in results)
    is_debian = any(r.get("Class") == "os-pkgs" and r.get("Type") == "debian" for r in results)

    art_exceptions = [e for e in exception_inventories if artifact in e.get("artifacts", [])]
    exc_findings = {
        (f["package"], f["license"]) for e in art_exceptions for f in e.get("findings", [])
    }

    unreviewed_os: set[str] = (
        set[str]() if art_exceptions else _find_unreviewed_os(results, config.allowed_base_families)
    )

    violations: list[dict[str, str]] = []
    total_pkgs = 0

    for r in results:
        target = r.get("Target", "")
        r_class = r.get("Class")
        is_os_pkg = (r_class == "os-pkgs") or (
            has_os and r_class == "license" and target == "OS Packages"
        )
        for pkg, lic in collect_package_items(r):
            total_pkgs += 1
            ctx = PackageContext(
                package=pkg,
                license_str=lic,
                is_os_pkg=is_os_pkg,
                is_debian=is_debian,
            )
            if is_allowed(ctx, config.policy) or (pkg, lic) in exc_findings:
                continue

            reviewed = any(
                rule.get("package") == pkg
                and rule.get("license") == lic
                and re.search(rule.get("artifactRegex", ".*"), artifact)
                and re.search(rule.get("targetRegex", ".*"), target)
                for rule in config.reviewed_rules
            )
            if reviewed:
                continue

            violations.append({
                "scanner": "trivy",
                "artifact": artifact,
                "target": target,
                "package": pkg,
                "license": lic,
            })

    return violations, unreviewed_os, total_pkgs


def main() -> int:
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd()))
    checks_dir = root / "src/bazel/checks/license_images"
    with (checks_dir / "policy.json").open(encoding="utf-8") as f:
        policy = json.load(f)
    with (checks_dir / "normalize-debian.yaml").open(encoding="utf-8") as f:
        norm_data = yaml.safe_load(f)
    with (checks_dir / "exceptions.yaml").open(encoding="utf-8") as f:
        exceptions_data = yaml.safe_load(f)

    inventories = load_exception_inventories(root, exceptions_data)
    if inventories is None:
        return 2

    config = AuditConfig(
        policy=LicensePolicy(
            allow_everywhere=set(policy.get("allowEverywhere", [])),
            allow_os=set(policy.get("allowOsPackages", [])),
            aliases={a["label"]: a["normalized"] for a in norm_data.get("aliases", [])},
            package_facts={
                (p["package"], p.get("reported", "UNKNOWN")): p["license"]
                for p in norm_data.get("packageFacts", [])
            },
        ),
        allowed_base_families={b["osType"] for b in norm_data.get("baseFamilies", [])},
        reviewed_rules=[
            r for r in policy.get("reviewedPackageLicenses", []) if r.get("scanner") == "trivy"
        ],
    )

    images = extract_base_images(root / "MODULE.bazel")
    report_dir = build_reports(root)
    if report_dir is None:
        return 2
    violations: list[dict[str, str]] = []
    unreviewed_os: set[str] = set()
    total_packages = 0

    for repo, image in sorted(images.items()):
        data = load_report(report_dir, repo, image)
        if data is None:
            return 2

        img_violations, img_unreviewed_os, img_pkgs = audit_image(image, data, inventories, config)
        violations.extend(img_violations)
        unreviewed_os.update(img_unreviewed_os)
        total_packages += img_pkgs

    if unreviewed_os or violations:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
