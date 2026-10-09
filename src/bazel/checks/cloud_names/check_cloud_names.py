#!/usr/bin/env python3
"""Validate cloud resource and container repository naming conventions across OpenTofu and Bazel sources."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import hcl2
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable

# Cloud object resource types and their name-bearing attributes.
TARGET_RESOURCES: dict[str, list[str]] = {
    "aws_secretsmanager_secret": ["name"],
    "aws_s3_bucket": ["bucket"],
    "aws_iam_role": ["name", "name_prefix"],
    "aws_iam_user": ["name"],
    "aws_iam_policy": ["name", "name_prefix"],
    "aws_iam_instance_profile": ["name", "name_prefix"],
    "aws_sqs_queue": ["name"],
    "aws_cloudwatch_event_rule": ["name"],
    "cloudflare_r2_bucket": ["name"],
}

# Cloud object resource types explicitly exempt from naming checks.
EXEMPT_RESOURCE_TYPES: frozenset[str] = frozenset({
    "aws_ecr_repository",
    "aws_ecr_repository_creation_template",
    "aws_glue_catalog_database",
})

IAM_RESOURCE_TYPES: frozenset[str] = frozenset({
    "aws_iam_role",
    "aws_iam_user",
    "aws_iam_policy",
    "aws_iam_instance_profile",
})

# Maximum permitted cloud object identifier length.
MAX_OBJECT_NAME_LENGTH = 48
ACCOUNT_ID_INTERPOLATION_LENGTH = 12

# Regex matching valid cluster name sources at the beginning of a template.
CLUSTER_NAME_SOURCE_RE = re.compile(
    r"^(?:"
    r"\$\{\s*(?:"
    r"var\.cluster_name|"
    r"local\.[a-zA-Z0-9_]*cluster[a-zA-Z0-9_]*\.name|"
    r"local\.[a-zA-Z0-9_]*cluster[a-zA-Z0-9_]*name|"
    r"module\.[a-zA-Z0-9_]+\.cluster_name|"
    r"var\.name_prefix|"
    r"module\.interface\.names\.cluster|"
    r"module\.interface\.names\.instance|"
    r"module\.interface\.names\.vpc"
    r")\s*\}|"
    r"ctrl-[a-z0-9-]+"
    r")"
)

# Regex matching pure pass-through of component inputs or interface maps.
PURE_PASSTHROUGH_RE = re.compile(
    r"^\$\{\s*(?:"
    r"var\.[a-zA-Z0-9_]+|"
    r"module\.interface\.names\.[a-zA-Z0-9_]+|"
    r"module\.interface\.names\[[^\]]+\]|"
    r"aws_iam_role\.[a-zA-Z0-9_]+\[[^\]]+\]\.name"
    r")\s*\}$"
)

# Regex matching valid bucket account ID interpolation at the end of the template.
ACCOUNT_SUFFIX_RE = re.compile(
    r"\$\{\s*(?:data\.aws_caller_identity\.[a-zA-Z0-9_]+\.account_id|var\.account_id|var\.aws_account_id|var\.account_suffix)\s*\}\"?$"
)

# Regex matching valid ECR repository paths: <ctrl cluster name>/<source path>
ECR_REPO_PATTERN = re.compile(r"^(?:\$\{[^}]+\}|[a-z0-9-]+)/(?:src/[a-z0-9_/-]+)$")

# Known interface token expansions for adjacent duplicate word checking.
KNOWN_INTERFACE_TOKENS = {
    "${module.interface.names.instance}": ["mesh", "router"],
}

IGNORED_PATH_SEGMENTS = frozenset({
    ".git",
    ".terraform",
    ".terragrunt-cache",
    ".tmp",
    ".venv",
    ".worktrees",
    "bazel-",
    "node_modules",
})


class CloudNamingError(Exception):
    """Raised when cloud object naming invariants are violated."""


@dataclass(frozen=True)
class Violation:
    """Represents a single static naming violation."""

    file: str
    target: str
    rule: str
    message: str


@dataclass(frozen=True)
class AllowlistEntry:
    """Represents an approved exception for an existing violation."""

    file: str
    target: str
    rule: str
    reason: str


def normalize_hcl_string(value: object) -> str:
    """Strip enclosing quotes and whitespace from an HCL string representation."""
    if isinstance(value, str):
        s = value.strip()
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            s = s[1:-1].strip()
        return s
    return str(value).strip()


def check_literal_characters(template: str, *, is_repository: bool = False) -> bool:
    """Verify that literal segments contain only lowercase letters, digits, and hyphens."""
    literals = re.sub(r"\$\{[^}]+\}", "", template)
    allowed_pattern = r"^[a-z0-9\-/]*$" if is_repository else r"^[a-z0-9\-]*$"
    return bool(re.match(allowed_pattern, literals))


def find_adjacent_duplicate_words(template: str) -> str | None:
    """Return the duplicate word if adjacent duplicate words occur in the template."""
    expanded = template
    for placeholder, words in KNOWN_INTERFACE_TOKENS.items():
        if placeholder in expanded:
            expanded = expanded.replace(placeholder, "-".join(words))

    stripped = re.sub(r"\$\{[^}]+\}", "", expanded)
    tokens = [t for t in re.split(r"[-_/]+", stripped) if t]
    for i in range(len(tokens) - 1):
        if tokens[i] and tokens[i] == tokens[i + 1]:
            return str(tokens[i])
    return None


def get_longest_cluster_name_length(repo_root: Path) -> int:
    """Extract the length of the longest cluster name across deployment manifests."""
    max_len = 13  # Baseline fallback
    deployments_dir = repo_root / "src/infra/terraform/deployments"
    if not deployments_dir.is_dir():
        return max_len

    for dep_yaml in deployments_dir.glob("**/deployment.yaml"):
        if any(seg in dep_yaml.parts for seg in IGNORED_PATH_SEGMENTS):
            continue
        try:
            with dep_yaml.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and "clusters" in doc:
            clusters = doc["clusters"]
            if isinstance(clusters, dict):
                for name in clusters:
                    max_len = max(max_len, len(name))
            elif isinstance(clusters, list):
                for c in clusters:
                    if isinstance(c, dict) and "name" in c:
                        max_len = max(max_len, len(c["name"]))
    return max_len


def get_longest_team_slug_length(repo_root: Path) -> int:
    """Extract the length of the longest team slug across team manifests."""
    max_len = 8  # Baseline fallback
    teams_dir = repo_root / "src/infra/definitions/teams"
    if not teams_dir.is_dir():
        return max_len

    for team_yaml in teams_dir.glob("*.yaml"):
        try:
            with team_yaml.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict):
            slug = doc.get("slug") or doc.get("name")
            if slug:
                max_len = max(max_len, len(slug))
    return max_len


def compute_template_max_length(
    template: str,
    *,
    max_cluster_len: int = 13,
    max_team_len: int = 8,
) -> int:
    """Compute the maximum expanded length of a name template using longest known interpolation values."""
    s = template
    # Replace cluster sources
    s = re.sub(r"\$\{\s*var\.cluster_name\s*\}", "X" * max_cluster_len, s)
    s = re.sub(
        r"\$\{\s*local\.[a-zA-Z0-9_]*cluster[a-zA-Z0-9_]*\.name\s*\}", "X" * max_cluster_len, s
    )
    s = re.sub(
        r"\$\{\s*local\.[a-zA-Z0-9_]*cluster[a-zA-Z0-9_]*name\s*\}", "X" * max_cluster_len, s
    )
    s = re.sub(r"\$\{\s*module\.[a-zA-Z0-9_]+\.cluster_name\s*\}", "X" * max_cluster_len, s)
    s = re.sub(r"\$\{\s*module\.interface\.names\.cluster\s*\}", "X" * max_cluster_len, s)
    s = re.sub(
        r"\$\{\s*module\.interface\.names\.instance\s*\}", "X" * (max_cluster_len + 12), s
    )  # mesh-router
    s = re.sub(r"\$\{\s*module\.interface\.names\.vpc\s*\}", "X" * (max_cluster_len + 4), s)  # -vpc
    s = re.sub(
        r"\$\{\s*module\.interface\.names\[each\.key\]\s*\}",
        "X" * (max_cluster_len + 1 + max_team_len),
        s,
    )
    s = re.sub(r"\$\{\s*var\.name_prefix\s*\}", "X" * max_cluster_len, s)
    # Account ID interpolation
    s = re.sub(
        r"\$\{\s*(?:data\.aws_caller_identity\.[a-zA-Z0-9_]+\.account_id|var\.account_id|var\.aws_account_id|var\.account_suffix)\s*\}",
        "X" * ACCOUNT_ID_INTERPOLATION_LENGTH,
        s,
    )
    # Team slugs
    s = re.sub(r"\$\{\s*(?:each\.key|each\.value|team)\s*\}", "X" * max_team_len, s)
    # Customer IAM name prefix defaults to empty in deployments
    s = re.sub(r"\$\{\s*var\.iam_name_prefix\s*\}", "", s)
    # Customer KMS alias prefix defaults to empty in deployments
    s = re.sub(r"\$\{\s*var\.kms_alias_prefix\s*\}", "", s)
    # Fallback for remaining generic interpolations
    s = re.sub(r"\$\{[^}]+\}", "X" * 8, s)
    return len(s)


def validate_kms_alias_name(
    template: str,
    *,
    max_cluster_len: int = 13,
    max_team_len: int = 8,
) -> list[tuple[str, str]]:
    """Validate a KMS key alias name template against alias/<cluster>-<purpose> rules."""
    errors: list[tuple[str, str]] = []
    if not template.startswith("alias/"):
        errors.append((
            "kms_alias_name",
            f"KMS alias name {template!r} must begin with 'alias/'",
        ))
    alias_body = template.removeprefix("alias/")
    alias_prefix_stripped = re.sub(r"^\$\{\s*var\.kms_alias_prefix\s*\}", "", alias_body)
    has_valid_alias_prefix = bool(
        CLUSTER_NAME_SOURCE_RE.match(alias_prefix_stripped)
        or PURE_PASSTHROUGH_RE.match(alias_prefix_stripped)
        or PURE_PASSTHROUGH_RE.match(alias_body)
        or alias_prefix_stripped.startswith("ctrl-")
    )
    if not has_valid_alias_prefix:
        errors.append((
            "kms_alias_name",
            f"KMS alias {template!r} must follow 'alias/<cluster>-<purpose>'",
        ))
    if not check_literal_characters(alias_body):
        errors.append((
            "lowercase_hyphens",
            f"KMS alias {template!r} literal parts must contain only lowercase alphanumeric characters and hyphens",
        ))
    dup = find_adjacent_duplicate_words(alias_body)
    if dup:
        errors.append((
            "no_duplicate_words",
            f"KMS alias {template!r} contains adjacent duplicate word {dup!r}",
        ))
    computed_len = compute_template_max_length(
        template, max_cluster_len=max_cluster_len, max_team_len=max_team_len
    )
    if computed_len > MAX_OBJECT_NAME_LENGTH:
        errors.append((
            "name_length",
            f"KMS alias {template!r} exceeds maximum length of 48 characters (computed length: {computed_len})",
        ))
    if not re.search(r"(?:kms_alias_prefix|kms_names|module\.interface\.names\.kms)", template):
        errors.append((
            "kms_alias_prefix_source",
            f"KMS alias {template!r} must be built through a naming source that can prepend kms_alias_prefix",
        ))
    return errors


def validate_cloud_name(
    template: str,
    *,
    is_bucket: bool = False,
    is_ecr: bool = False,
    is_glue: bool = False,
    is_secret: bool = False,
    is_iam: bool = False,
    is_kms_alias: bool = False,
    max_cluster_len: int = 13,
    max_team_len: int = 8,
) -> list[tuple[str, str]]:
    """Validate a cloud object name template string against core naming rules."""
    if is_kms_alias:
        return validate_kms_alias_name(
            template, max_cluster_len=max_cluster_len, max_team_len=max_team_len
        )

    if is_ecr or is_glue:
        # ECR repositories and Glue catalog database names are exempt.
        return []

    errors: list[tuple[str, str]] = []

    # Rule 1: Starts with cluster name or pure pass-through
    prefix_stripped = re.sub(r"^\$\{\s*var\.iam_name_prefix\s*\}", "", template)
    has_valid_prefix = bool(
        CLUSTER_NAME_SOURCE_RE.match(prefix_stripped)
        or PURE_PASSTHROUGH_RE.match(template)
        or prefix_stripped.startswith("ctrl-")
    )
    if not has_valid_prefix:
        errors.append((
            "cluster_prefix",
            f"Name {template!r} must begin with an interpolation of a cluster-name source or 'ctrl-'",
        ))

    # Rule 2: Literal parts contain only [a-z0-9-] (no uppercase, no underscores)
    if not check_literal_characters(template):
        errors.append((
            "lowercase_hyphens",
            f"Name {template!r} literal parts must contain only lowercase alphanumeric characters and hyphens",
        ))

    # Rule 3: No adjacent duplicate words
    dup = find_adjacent_duplicate_words(template)
    if dup:
        errors.append((
            "no_duplicate_words",
            f"Name {template!r} contains adjacent duplicate word {dup!r}",
        ))

    # Rule 4: Maximum length at most 48 characters
    computed_len = compute_template_max_length(
        template, max_cluster_len=max_cluster_len, max_team_len=max_team_len
    )
    if computed_len > MAX_OBJECT_NAME_LENGTH:
        errors.append((
            "name_length",
            f"Name {template!r} exceeds maximum length of 48 characters (computed length: {computed_len})",
        ))

    # Rule 5: Bucket names must end with an AWS account ID interpolation
    if is_bucket and not ACCOUNT_SUFFIX_RE.search(template):
        errors.append((
            "bucket_account_suffix",
            f"Bucket name {template!r} must end with an account ID interpolation (${{...account_id}})",
        ))

    # Rule 6: Secrets must not contain redundant resource type suffix '-secrets' or generic placeholder
    if is_secret and template.endswith(("-platform-secrets", "-secrets")):
        errors.append((
            "secret_name_redundancy",
            f"Secret name {template!r} contains redundant type suffix '-secrets' or generic placeholder",
        ))

    # Rule 7: IAM objects must be built through a single naming source prepending iam_name_prefix
    if is_iam and not re.search(
        r"(?:iam_name_prefix|iam_names|module\.interface\.names\[|module\.interface\.names\.iam)",
        template,
    ):
        errors.append((
            "iam_name_prefix_source",
            f"IAM object name {template!r} must be built through a naming source that can prepend iam_name_prefix",
        ))

    return errors


def _inspect_target_resources(
    data: dict[str, Any],
    rel_path: str,
    max_cluster_len: int,
    max_team_len: int,
) -> list[Violation]:
    """Inspect name-bearing attributes of target cloud resource blocks."""
    violations: list[Violation] = []
    for res_dict in data.get("resource", []):
        for raw_type, insts in res_dict.items():
            res_type = raw_type.strip('"')
            if res_type in EXEMPT_RESOURCE_TYPES or res_type not in TARGET_RESOURCES:
                continue
            attrs = TARGET_RESOURCES[res_type]
            for raw_inst, cfg in insts.items():
                inst = raw_inst.strip('"')
                for attr in attrs:
                    if attr not in cfg:
                        continue
                    val = normalize_hcl_string(cfg[attr])
                    target = f"{res_type}.{inst}.{attr}"
                    is_bucket = res_type in {"aws_s3_bucket", "cloudflare_r2_bucket"}
                    is_secret = res_type == "aws_secretsmanager_secret"
                    is_iam = res_type in IAM_RESOURCE_TYPES

                    errors = validate_cloud_name(
                        val,
                        is_bucket=is_bucket,
                        is_secret=is_secret,
                        is_iam=is_iam,
                        max_cluster_len=max_cluster_len,
                        max_team_len=max_team_len,
                    )
                    for rule, msg in errors:
                        violations.append(Violation(rel_path, target, rule, msg))
    return violations


def _collect_kms_aliases(
    insts: dict[str, Any],
    rel_path: str,
    kms_aliases: set[str],
    kms_alias_targets: set[str],
    violations: list[Violation],
    max_cluster_len: int,
    max_team_len: int,
) -> None:
    """Helper to record KMS aliases and inspect their names."""
    for raw_inst, cfg in insts.items():
        inst = raw_inst.strip('"')
        kms_aliases.add(inst)
        if not isinstance(cfg, dict):
            continue
        if "target_key_id" in cfg:
            target_ref = normalize_hcl_string(cfg["target_key_id"])
            match = re.search(r"aws_kms_key\.([a-zA-Z0-9_]+)", target_ref)
            if match:
                kms_alias_targets.add(match.group(1))
        if "name" in cfg:
            val = normalize_hcl_string(cfg["name"])
            target = f"aws_kms_alias.{inst}.name"
            errors = validate_kms_alias_name(
                val,
                max_cluster_len=max_cluster_len,
                max_team_len=max_team_len,
            )
            for rule, msg in errors:
                violations.append(Violation(rel_path, target, rule, msg))


def _inspect_kms_resources(
    data: dict[str, Any],
    rel_path: str,
    max_cluster_len: int,
    max_team_len: int,
) -> list[Violation]:
    """Inspect KMS keys to ensure each has an associated alias matching alias/<cluster>-<purpose>."""
    violations: list[Violation] = []
    kms_keys: set[str] = set()
    kms_aliases: set[str] = set()
    kms_alias_targets: set[str] = set()

    for res_dict in data.get("resource", []):
        for raw_type, insts in res_dict.items():
            res_type = raw_type.strip('"')
            if res_type == "aws_kms_key":
                kms_keys.update(raw_inst.strip('"') for raw_inst in insts)
            elif res_type == "aws_kms_alias":
                _collect_kms_aliases(
                    insts,
                    rel_path,
                    kms_aliases,
                    kms_alias_targets,
                    violations,
                    max_cluster_len,
                    max_team_len,
                )

    # Verify that every KMS key has an associated alias
    for key_inst in sorted(kms_keys):
        if key_inst not in kms_aliases and key_inst not in kms_alias_targets:
            violations.append(
                Violation(
                    rel_path,
                    f"aws_kms_key.{key_inst}",
                    "kms_key_missing_alias",
                    f"KMS key '{key_inst}' must have an associated aws_kms_alias named 'alias/<cluster>-<purpose>'",
                )
            )

    return violations


def _inspect_module_calls(
    data: dict[str, Any],
    rel_path: str,
    max_cluster_len: int,
    max_team_len: int,
) -> list[Violation]:
    """Inspect module invocation inputs that define cloud object or repository names."""
    violations: list[Violation] = []
    for mod_dict in data.get("module", []):
        for raw_mod, cfg in mod_dict.items():
            mod = raw_mod.strip('"')

            if "secret_name" in cfg:
                val = normalize_hcl_string(cfg["secret_name"])
                target = f"module.{mod}.secret_name"
                errors = validate_cloud_name(
                    val,
                    is_secret=True,
                    max_cluster_len=max_cluster_len,
                    max_team_len=max_team_len,
                )
                for rule, msg in errors:
                    violations.append(Violation(rel_path, target, rule, msg))

            if "roles" in cfg:
                roles_val = cfg["roles"]
                keys: list[str] = []
                if isinstance(roles_val, dict):
                    keys = [str(k).strip('"') for k in roles_val]
                elif isinstance(roles_val, str):
                    keys = [str(k) for k in re.findall(r"([a-zA-Z0-9_]+)\s*=\s*\{", roles_val)]
                for k in sorted(set(keys)):
                    if "_" in k:
                        violations.append(
                            Violation(
                                rel_path,
                                f"role_key:{k}",
                                "map_key_hyphenation",
                                f"Role map key {k!r} contains underscores without hyphenation",
                            )
                        )
    return violations


def inspect_terraform_file(
    file_path: Path,
    repo_root: Path,
    *,
    max_cluster_len: int = 13,
    max_team_len: int = 8,
) -> list[Violation]:
    """Inspect one Terraform / OpenTofu file for cloud object naming violations."""
    rel_path = file_path.relative_to(repo_root).as_posix()
    violations: list[Violation] = []

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = hcl2.load(f)
    except Exception as e:
        violations.append(
            Violation(rel_path, "file", "hcl_parse_error", f"Failed to parse HCL: {e}")
        )
        return violations

    violations.extend(_inspect_target_resources(data, rel_path, max_cluster_len, max_team_len))
    violations.extend(_inspect_kms_resources(data, rel_path, max_cluster_len, max_team_len))
    violations.extend(_inspect_module_calls(data, rel_path, max_cluster_len, max_team_len))

    return violations


def inspect_build_file(_file_path: Path, _repo_root: Path) -> list[Violation]:
    """Inspect one Bazel BUILD.bazel file for OCI workload publish repository naming."""
    # OCI publish rules are exempt (rule amended).
    return []


def _inspect_deployment_tags_variable(
    parsed_files: list[tuple[Path, str, dict[str, Any]]],
    rel_dir: str,
    rep_file: str,
) -> list[Violation]:
    violations: list[Violation] = []
    tags_var_found = False
    tags_var_file: str | None = None
    tags_var_type_valid = False
    actual_var_type: str | None = None

    for _, rel_path, data in parsed_files:
        for v_dict in data.get("variable", []):
            for raw_var_name, cfg in v_dict.items():
                if raw_var_name.strip('"') == "tags":
                    tags_var_found = True
                    tags_var_file = rel_path
                    if isinstance(cfg, dict):
                        raw_type = normalize_hcl_string(cfg.get("type", ""))
                        actual_var_type = raw_type
                        if re.search(r"map\s*\(\s*string\s*\)", raw_type) or raw_type == "map":
                            tags_var_type_valid = True
                    break
            if tags_var_found:
                break
        if tags_var_found:
            break

    if not tags_var_found:
        violations.append(
            Violation(
                rep_file,
                "variable.tags",
                "deployment_tags",
                f"Deployment '{rel_dir}' must declare a 'tags' input variable of type map(string)",
            )
        )
    elif not tags_var_type_valid:
        violations.append(
            Violation(
                tags_var_file or rep_file,
                "variable.tags",
                "deployment_tags",
                f"Variable 'tags' in '{rel_dir}' must be of type map(string), got {actual_var_type!r}",
            )
        )
    return violations


def _inspect_provider_default_tags(
    parsed_files: list[tuple[Path, str, dict[str, Any]]],
) -> list[Violation]:
    violations: list[Violation] = []
    for _, rel_path, data in parsed_files:
        for p_dict in data.get("provider", []):
            for raw_p_name, p_cfg in p_dict.items():
                if raw_p_name.strip('"') != "aws" or not isinstance(p_cfg, dict):
                    continue
                default_tags = p_cfg.get("default_tags")
                if not default_tags:
                    violations.append(
                        Violation(
                            rel_path,
                            "provider.aws.default_tags",
                            "deployment_tags",
                            f"AWS provider in '{rel_path}' must configure default_tags block",
                        )
                    )
                    continue

                dt_list = default_tags if isinstance(default_tags, list) else [default_tags]
                has_var_tags = any(
                    isinstance(dt, dict)
                    and "tags" in dt
                    and isinstance(dt["tags"], str)
                    and bool(re.search(r"(?:var\.)?tags", normalize_hcl_string(dt["tags"])))
                    for dt in dt_list
                )
                if not has_var_tags:
                    violations.append(
                        Violation(
                            rel_path,
                            "provider.aws.default_tags.tags",
                            "deployment_tags",
                            f"AWS provider default_tags in '{rel_path}' must set tags = var.tags",
                        )
                    )
    return violations


def _inspect_cell_annotations(
    cells: list[Any],
    rel_path: str,
    m_name: str,
) -> list[Violation]:
    violations: list[Violation] = []
    for idx, cell in enumerate(cells):
        if not isinstance(cell, dict):
            continue
        cell_name = normalize_hcl_string(cell.get("name", f"cell_{idx}"))
        raw_cell_ann = cell.get("annotations")
        if not isinstance(raw_cell_ann, dict):
            continue
        cell_ann = {str(k).strip('"'): v for k, v in raw_cell_ann.items()}
        target = f"module.{m_name}.registered_cells[{cell_name}].annotations[resource-tags]"
        if "resource-tags" not in cell_ann:
            violations.append(
                Violation(
                    rel_path,
                    target,
                    "deployment_tags",
                    f"Registered cell '{cell_name}' annotations in module '{m_name}' must include 'resource-tags'",
                )
            )
        else:
            rt_val = normalize_hcl_string(cell_ann["resource-tags"])
            if not re.search(r"jsonencode\(\s*(?:var\.)?tags\s*\)", rt_val):
                violations.append(
                    Violation(
                        rel_path,
                        target,
                        "deployment_tags",
                        f"'resource-tags' annotation for cell '{cell_name}' in module '{m_name}' must be jsonencode(var.tags), got {rt_val!r}",
                    )
                )
    return violations


def _inspect_module_annotations(
    parsed_files: list[tuple[Path, str, dict[str, Any]]],
) -> list[Violation]:
    violations: list[Violation] = []
    for _, rel_path, data in parsed_files:
        for m_dict in data.get("module", []):
            for raw_m_name, m_cfg in m_dict.items():
                m_name = raw_m_name.strip('"')
                if not isinstance(m_cfg, dict):
                    continue
                if "annotations" in m_cfg:
                    raw_ann = m_cfg["annotations"]
                    if isinstance(raw_ann, dict):
                        ann = {str(k).strip('"'): v for k, v in raw_ann.items()}
                        is_registration = (
                            m_name == "control_plane"
                            or "control_plane" in m_name
                            or any(
                                k in ann
                                for k in (
                                    "control-gateway-ipv4",
                                    "service-cidr",
                                    "intranet-domain",
                                    "public-domain",
                                    "registered-cells",
                                    "resource-prefix",
                                )
                            )
                            or "resource-tags" in ann
                        )
                        if is_registration:
                            target = f"module.{m_name}.annotations[resource-tags]"
                            if "resource-tags" not in ann:
                                violations.append(
                                    Violation(
                                        rel_path,
                                        target,
                                        "deployment_tags",
                                        f"Cluster registration annotations in module '{m_name}' must include 'resource-tags'",
                                    )
                                )
                            else:
                                rt_val = normalize_hcl_string(ann["resource-tags"])
                                if not re.search(r"jsonencode\(\s*(?:var\.)?tags\s*\)", rt_val):
                                    violations.append(
                                        Violation(
                                            rel_path,
                                            target,
                                            "deployment_tags",
                                            f"'resource-tags' annotation in module '{m_name}' must be jsonencode(var.tags), got {rt_val!r}",
                                        )
                                    )

                if "registered_cells" in m_cfg and isinstance(m_cfg["registered_cells"], list):
                    violations.extend(
                        _inspect_cell_annotations(m_cfg["registered_cells"], rel_path, m_name)
                    )
    return violations


def _is_non_cluster_deployment(
    deployment_dir: Path, has_aws_provider: bool, has_cluster_registration: bool
) -> bool:
    dep_yaml = deployment_dir / "deployment.yaml"
    if not dep_yaml.is_file():
        return False
    try:
        with dep_yaml.open("r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
            if isinstance(doc, dict) and "clusters" in doc:
                return False
    except (OSError, yaml.YAMLError):
        return False
    return not has_aws_provider and not has_cluster_registration


def inspect_deployment(deployment_dir: Path, repo_root: Path) -> list[Violation]:
    """Validate that a deployment declares a tags variable used in provider default_tags and cluster annotations."""
    rel_dir = deployment_dir.relative_to(repo_root).as_posix()
    tf_files = sorted(deployment_dir.glob("*.tf"))
    if not tf_files:
        return []

    parsed_files: list[tuple[Path, str, dict[str, Any]]] = []
    has_aws_provider = False
    has_cluster_registration = False

    for tf_file in tf_files:
        if any(seg in tf_file.parts for seg in IGNORED_PATH_SEGMENTS):
            continue
        rel_path = tf_file.relative_to(repo_root).as_posix()
        try:
            with tf_file.open("r", encoding="utf-8") as f:
                data = hcl2.load(f)
                parsed_files.append((tf_file, rel_path, data))
        except (OSError, Exception) as e:
            return [Violation(rel_path, "file", "hcl_parse_error", f"Failed to parse HCL: {e}")]

        for p_dict in data.get("provider", []):
            if any(k.strip('"') == "aws" for k in p_dict):
                has_aws_provider = True

        for m_dict in data.get("module", []):
            for _, m_cfg in m_dict.items():
                if isinstance(m_cfg, dict) and (
                    "annotations" in m_cfg or "registered_cells" in m_cfg
                ):
                    has_cluster_registration = True

    if _is_non_cluster_deployment(deployment_dir, has_aws_provider, has_cluster_registration):
        return []

    rep_file = parsed_files[0][1] if parsed_files else rel_dir
    violations: list[Violation] = []
    violations.extend(_inspect_deployment_tags_variable(parsed_files, rel_dir, rep_file))
    violations.extend(_inspect_provider_default_tags(parsed_files))
    violations.extend(_inspect_module_annotations(parsed_files))
    return violations


def load_allowlist(allowlist_path: Path) -> dict[tuple[str, str, str], str]:
    """Load the explicit allowlist mapping (file, target, rule) to rationale."""
    if not allowlist_path.is_file():
        return {}

    with allowlist_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or not isinstance(data, dict):
        return {}

    entries: dict[tuple[str, str, str], str] = {}
    for item in data.get("violations", []):
        key = (item["file"], item["target"], item["rule"])
        entries[key] = item["reason"]
    return entries


def find_allowlist_path(repo_root: Path) -> Path:
    """Resolve allowlist path from workspace root or package directory."""
    candidate1 = repo_root / "src/bazel/checks/cloud_names/allowlist.yaml"
    if candidate1.is_file():
        return candidate1
    candidate2 = Path(__file__).resolve().parent / "allowlist.yaml"
    if candidate2.is_file():
        return candidate2
    return candidate1


def _scan_terraform_files(
    repo_root: Path, max_cluster_len: int, max_team_len: int
) -> list[Violation]:
    tf_dir = repo_root / "src/infra/terraform"
    if not tf_dir.is_dir():
        return []
    violations: list[Violation] = []
    for tf_file in sorted(tf_dir.rglob("*.tf")):
        if any(seg in tf_file.parts for seg in IGNORED_PATH_SEGMENTS):
            continue
        violations.extend(
            inspect_terraform_file(
                tf_file,
                repo_root,
                max_cluster_len=max_cluster_len,
                max_team_len=max_team_len,
            )
        )
    return violations


def _scan_deployment_directories(repo_root: Path) -> list[Violation]:
    deployments_dir = repo_root / "src/infra/terraform/deployments"
    if not deployments_dir.is_dir():
        return []
    violations: list[Violation] = []
    for dep_dir in sorted(deployments_dir.iterdir()):
        if not dep_dir.is_dir() or any(seg in dep_dir.parts for seg in IGNORED_PATH_SEGMENTS):
            continue
        violations.extend(inspect_deployment(dep_dir, repo_root))
    return violations


def _scan_build_files(repo_root: Path) -> list[Violation]:
    src_dir = repo_root / "src"
    if not src_dir.is_dir():
        return []
    violations: list[Violation] = []
    for build_file in sorted(src_dir.rglob("BUILD.bazel")):
        if any(seg in build_file.parts for seg in IGNORED_PATH_SEGMENTS):
            continue
        violations.extend(inspect_build_file(build_file, repo_root))
    return violations


def scan_workspace(
    repo_root: Path,
    allowlist_path: Path | None = None,
) -> tuple[list[Violation], list[tuple[str, str, str]]]:
    """Scan Terraform sources and Bazel BUILD files for cloud naming violations."""
    max_cluster_len = get_longest_cluster_name_length(repo_root)
    max_team_len = get_longest_team_slug_length(repo_root)

    all_violations: list[Violation] = []
    all_violations.extend(_scan_terraform_files(repo_root, max_cluster_len, max_team_len))
    all_violations.extend(_scan_deployment_directories(repo_root))
    all_violations.extend(_scan_build_files(repo_root))

    # Filter against allowlist
    resolved_allowlist_path = allowlist_path or find_allowlist_path(repo_root)
    allowlist = load_allowlist(resolved_allowlist_path)

    unapproved: list[Violation] = []
    used_allowlist_keys: set[tuple[str, str, str]] = set()

    for v in all_violations:
        key = (v.file, v.target, v.rule)
        if key in allowlist:
            used_allowlist_keys.add(key)
        else:
            unapproved.append(v)

    stale_keys = [k for k in allowlist if k not in used_allowlist_keys]

    return unapproved, stale_keys


def _enter_workspace() -> None:
    """Enter workspace directory if invoked via bazel run."""
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        os.chdir(workspace)


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint for cloud object naming validation."""
    _enter_workspace()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", ".")),
        help="Repository root directory (defaults to BUILD_WORKSPACE_DIRECTORY or cwd)",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Path to allowlist YAML file",
    )
    args = parser.parse_args(argv)

    repo_root = args.root.resolve()
    unapproved, stale_keys = scan_workspace(repo_root, args.allowlist)

    if stale_keys:
        print(f"Warning: {len(stale_keys)} stale allowlist entries found:", file=sys.stderr)
        for f, t, r in sorted(stale_keys):
            print(f"  - file: {f}\n    target: {t}\n    rule: {r}", file=sys.stderr)

    if unapproved:
        print(
            f"Error: Found {len(unapproved)} unapproved cloud object naming violations:",
            file=sys.stderr,
        )
        for v in unapproved:
            print(f"  {v.file}:{v.target} [{v.rule}]: {v.message}", file=sys.stderr)
        return 1

    print("Cloud object naming check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
