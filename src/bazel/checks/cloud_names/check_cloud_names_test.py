#!/usr/bin/env python3
"""Table-driven unit tests for cloud object naming rules, defect detection, and allowlisting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_cloud_names import (
    check_literal_characters,
    find_adjacent_duplicate_words,
    inspect_build_file,
    inspect_deployment,
    inspect_terraform_file,
    scan_workspace,
    validate_cloud_name,
)


class CloudNamingTableTest(unittest.TestCase):
    """Table-driven tests verifying accepting and rejecting cases for each cloud naming invariant."""

    def test_missing_cluster_name_prefix_is_rejected(self) -> None:
        """Verify that cloud object names must start with an approved cluster name interpolation."""
        cases = [
            # Accepting cases
            ("${var.cluster_name}-opencost", True),
            ("${local.ctrl_cluster.name}-argocd-git-deploy-key", True),
            ("${module.cluster.cluster_name}-nodes", True),
            ("${var.name_prefix}-service", True),
            ("${module.interface.names.cluster}-nodes", True),
            ("${module.interface.names.instance}-tailscale-auth-key", True),
            ("${module.interface.names.vpc}-flow-logs", True),
            ("${module.interface.names[each.key]}", True),
            ("${module.interface.names.secret}", True),
            ("${var.secret_name}", True),
            ("ctrl-aws-usw2-global", True),
            # Rejecting cases
            ("buildbuddy-auth-${var.cluster_name}", False),
            ("KarpenterNodeInstanceProfile-${module.interface.names.cluster}", False),
            ("EBSCSI-${module.interface.names.cluster}", False),
            ("${var.bucket_prefix}-my-bucket", False),
            ("${var.installation_name}-my-bucket", False),
            ("standalone-unprefixed-object", False),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                errors = [rule for rule, _ in validate_cloud_name(template)]
                has_prefix_error = "cluster_prefix" in errors
                self.assertEqual(not has_prefix_error, expected_valid, f"Template: {template}")

    def test_uppercase_and_underscores_in_literal_parts_are_rejected(self) -> None:
        """Verify that literal segments contain only lowercase alphanumeric characters and hyphens."""
        cases = [
            # Accepting cases
            ("${var.cluster_name}-s3-gateway-config", True),
            ("${var.cluster_name}-karpenter-spot-interruption", True),
            ("${var.cluster_name}-valid-name-123", True),
            # Rejecting cases
            ("${var.cluster_name}-MyService", False),
            ("${var.cluster_name}-my_service", False),
            ("${var.cluster_name}-my@service", False),
            ("KarpenterNodeInstanceProfile-${var.cluster_name}", False),
            ("EBSCSI-${var.cluster_name}", False),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                is_valid = check_literal_characters(template)
                self.assertEqual(is_valid, expected_valid, f"Template: {template}")

    def test_adjacent_duplicate_words_are_rejected(self) -> None:
        """Verify that adjacent duplicate words like router-router are detected and rejected."""
        cases = [
            # Accepting cases
            ("${var.cluster_name}-mesh-router", None),
            ("${var.cluster_name}-cluster-nodes", None),
            ("${module.interface.names.cluster}-cluster", None),
            # Rejecting cases
            ("${var.cluster_name}-mesh-router-router", "router"),
            ("${module.interface.names.instance}-router", "router"),
            ("${var.cluster_name}-gateway-gateway", "gateway"),
            ("${var.cluster_name}-node-node-pool", "node"),
        ]
        for template, expected_dup in cases:
            with self.subTest(template=template):
                dup = find_adjacent_duplicate_words(template)
                self.assertEqual(dup, expected_dup, f"Template: {template}")

    def test_bucket_missing_account_id_interpolation_is_rejected(self) -> None:
        """Verify that S3 and R2 bucket names must terminate with an account ID interpolation."""
        cases = [
            # Accepting cases
            ("${var.cluster_name}-data-${data.aws_caller_identity.current.account_id}", True),
            ("${var.cluster_name}-storage-${var.account_id}", True),
            ("${var.cluster_name}-logs-${var.aws_account_id}", True),
            ("${var.cluster_name}-backups-${var.account_suffix}", True),
            # Rejecting cases
            ("${var.cluster_name}-billing-reports", False),
            ("${var.cluster_name}-billing-access-logs", False),
            ("${var.cluster_name}-global-team-000000000000", False),
            ("${var.cluster_name}-scratch-data", False),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                errors = [rule for rule, _ in validate_cloud_name(template, is_bucket=True)]
                has_suffix_error = "bucket_account_suffix" in errors
                self.assertEqual(not has_suffix_error, expected_valid, f"Template: {template}")

    def test_secret_with_redundant_secrets_suffix_is_rejected(self) -> None:
        """Verify that secret names cannot end with redundant type suffix -secrets or generic placeholder."""
        cases = [
            # Accepting cases
            ("${var.cluster_name}-s3-gateway-config", True),
            ("${var.cluster_name}-tailscale-auth-key", True),
            ("${var.cluster_name}-tailscale-operator-oauth", True),
            # Rejecting cases
            ("${var.cluster_name}-platform-secrets", False),
            ("${var.cluster_name}-database-secrets", False),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                errors = [rule for rule, _ in validate_cloud_name(template, is_secret=True)]
                has_redundancy_error = "secret_name_redundancy" in errors
                self.assertEqual(not has_redundancy_error, expected_valid, f"Template: {template}")

    def test_ecr_repository_is_exempt_from_naming_rules(self) -> None:
        """Verify that ECR repositories and creation templates are exempt from naming checks."""
        cases = [
            "ctrl-aws-usw2/src/infra/tools/coder_snapshot_portal",
            "${var.cluster_name}/src/examples/batch_cron",
            "src/infra/tools/coder_snapshot_portal",
            "infrastructure",
            "unprefixed/image",
        ]
        for template in cases:
            with self.subTest(template=template):
                errors = validate_cloud_name(template, is_ecr=True)
                self.assertEqual(errors, [], f"Template: {template}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            tf_file = repo_root / "src/infra/terraform/ecr.tf"
            tf_file.parent.mkdir(parents=True, exist_ok=True)
            tf_file.write_text(
                """
resource "aws_ecr_repository" "repo" {
  name = "arbitrary-source-path"
}

resource "aws_ecr_repository_creation_template" "tpl" {
  prefix = "src"
}
""",
                encoding="utf-8",
            )
            violations = inspect_terraform_file(tf_file, repo_root)
            self.assertEqual(len(violations), 0)

    def test_terraform_file_inspection_flags_violations(self) -> None:
        """Verify that OpenTofu file parsing identifies invalid resource names."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            tf_file = repo_root / "src/infra/terraform/bad_resource.tf"
            tf_file.parent.mkdir(parents=True, exist_ok=True)
            tf_file.write_text(
                """
resource "aws_iam_instance_profile" "bad" {
  name = "BadProfileName-${var.cluster_name}"
}

resource "aws_s3_bucket" "no_account" {
  bucket = "${var.cluster_name}-no-account"
}
""",
                encoding="utf-8",
            )
            violations = inspect_terraform_file(tf_file, repo_root)
            rules = {v.rule for v in violations}
            self.assertIn("cluster_prefix", rules)
            self.assertIn("lowercase_hyphens", rules)
            self.assertIn("bucket_account_suffix", rules)

    def test_build_file_oci_publish_rules_are_exempt(self) -> None:
        """Verify that Bazel BUILD.bazel OCI workload publish targets are exempt."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            build_file = repo_root / "src/infra/tools/sample/BUILD.bazel"
            build_file.parent.mkdir(parents=True, exist_ok=True)
            build_file.write_text(
                """
load("//src/bazel/rules/oci:defs.bzl", "oci_workload_publish")

oci_workload_publish(
    name = "publish",
    image = ":image",
    repository_path = "src/infra/tools/sample",
)
""",
                encoding="utf-8",
            )
            violations = inspect_build_file(build_file, repo_root)
            self.assertEqual(len(violations), 0)

    def test_allowlist_suppresses_approved_violations(self) -> None:
        """Verify that allowlist entries suppress matching violations while flagging stale entries."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            tf_file = repo_root / "src/infra/terraform/fixture.tf"
            tf_file.parent.mkdir(parents=True, exist_ok=True)
            tf_file.write_text(
                """
resource "aws_s3_bucket" "test" {
  bucket = "${var.cluster_name}-test-bucket"
}
""",
                encoding="utf-8",
            )
            allowlist_file = repo_root / "allowlist.yaml"
            allowlist_file.write_text(
                """
violations:
  - file: src/infra/terraform/fixture.tf
    target: aws_s3_bucket.test.bucket
    rule: bucket_account_suffix
    reason: "Approved test exception"
  - file: src/infra/terraform/stale.tf
    target: nonexistent
    rule: bucket_account_suffix
    reason: "Stale test exception"
""",
                encoding="utf-8",
            )
            unapproved, stale_keys = scan_workspace(repo_root, allowlist_file)
            self.assertEqual(len(unapproved), 0)
            self.assertEqual(len(stale_keys), 1)
            self.assertEqual(stale_keys[0][0], "src/infra/terraform/stale.tf")

    def test_name_exceeding_48_characters_is_rejected(self) -> None:
        """Verify that cloud object names longer than 48 characters computed with max interpolation are rejected."""
        cases = [
            # Accepting cases (<= 48 characters)
            ("${var.cluster_name}-opencost", True),
            ("${var.cluster_name}-karpenter-instance-state-change", True),
            ("${var.cluster_name}-${each.key}-storage", True),
            ("${var.cluster_name}-bucket-${var.account_id}", True),
            # Rejecting cases (> 48 characters)
            ("${var.cluster_name}-a-really-extremely-excessively-long-workload-name", False),
            ("${var.cluster_name}-${each.key}-very-long-extra-identifier-name", False),
            (
                "${var.cluster_name}-database-storage-volume-cluster-filesystem-data-${var.account_id}",
                False,
            ),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                errors = [rule for rule, _ in validate_cloud_name(template)]
                has_length_error = "name_length" in errors
                self.assertEqual(not has_length_error, expected_valid, f"Template: {template}")

    def test_kms_key_without_alias_is_rejected(self) -> None:
        """Verify that every aws_kms_key must have an associated aws_kms_alias."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            # Case 1: KMS key without alias
            tf_bad = repo_root / "src/infra/terraform/bad_kms.tf"
            tf_bad.parent.mkdir(parents=True, exist_ok=True)
            tf_bad.write_text(
                """
resource "aws_kms_key" "unaliased" {
  description = "No alias"
}
""",
                encoding="utf-8",
            )
            violations = inspect_terraform_file(tf_bad, repo_root)
            rules = {v.rule for v in violations}
            self.assertIn("kms_key_missing_alias", rules)

            # Case 2: KMS key with valid alias
            tf_good = repo_root / "src/infra/terraform/good_kms.tf"
            tf_good.write_text(
                """
resource "aws_kms_key" "my_key" {
  description = "With alias"
}

resource "aws_kms_alias" "my_key" {
  name          = "alias/${var.kms_alias_prefix}${var.cluster_name}-my-key"
  target_key_id = aws_kms_key.my_key.key_id
}
""",
                encoding="utf-8",
            )
            violations_good = inspect_terraform_file(tf_good, repo_root)
            self.assertEqual(len(violations_good), 0)

    def test_kms_alias_naming_rules(self) -> None:
        """Verify that KMS aliases must follow alias/<cluster>-<purpose> and include kms_alias_prefix."""
        cases = [
            # Accepting cases
            ("alias/${var.kms_alias_prefix}${var.cluster_name}-storage", True),
            ("alias/${module.interface.names.kms_key}", True),
            ("alias/${var.kms_alias_prefix}${var.secret_name}", True),
            # Rejecting cases
            ("${var.kms_alias_prefix}${var.cluster_name}-storage", False),
            ("alias/noncluster-storage", False),
            ("alias/${var.cluster_name}-storage", False),
            ("alias/${var.iam_name_prefix}${var.cluster_name}-storage", False),
            ("alias/${var.kms_alias_prefix}${var.cluster_name}-my_storage", False),
            ("alias/${var.kms_alias_prefix}${var.cluster_name}-router-router", False),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                errors = validate_cloud_name(template, is_kms_alias=True)
                self.assertEqual(
                    len(errors) == 0, expected_valid, f"Template: {template}, Errors: {errors}"
                )

    def test_kms_alias_without_kms_alias_prefix_source_is_rejected(self) -> None:
        """Verify that KMS alias names must be built through a naming source prepending kms_alias_prefix."""
        cases = [
            # Accepting cases
            ("alias/${var.kms_alias_prefix}${var.cluster_name}-storage", True),
            ("alias/${module.interface.names.kms_key}", True),
            # Rejecting cases
            ("alias/${var.cluster_name}-storage", False),
            ("alias/${var.iam_name_prefix}${var.cluster_name}-storage", False),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                errors = [rule for rule, _ in validate_cloud_name(template, is_kms_alias=True)]
                has_prefix_error = "kms_alias_prefix_source" in errors
                self.assertEqual(not has_prefix_error, expected_valid, f"Template: {template}")

    def test_iam_object_without_iam_name_prefix_source_is_rejected(self) -> None:
        """Verify that IAM object names must be built through a naming source prepending iam_name_prefix."""
        cases = [
            # Accepting cases
            ("${var.iam_name_prefix}${var.cluster_name}-role", True),
            ("${module.interface.names.iam_role}", True),
            ("${module.interface.names[each.key]}", True),
            # Rejecting cases
            ("${var.cluster_name}-role", False),
            ("${module.interface.names.cluster}-nodes", False),
            ("${var.cluster_name}-opencost", False),
        ]
        for template, expected_valid in cases:
            with self.subTest(template=template):
                errors = [rule for rule, _ in validate_cloud_name(template, is_iam=True)]
                has_iam_error = "iam_name_prefix_source" in errors
                self.assertEqual(not has_iam_error, expected_valid, f"Template: {template}")

    def test_glue_catalog_database_is_exempt_from_naming_rules(self) -> None:
        """Verify that Glue catalog database names are exempt from naming rules."""
        cases = [
            "ctrl_aws_usw2_cur",
            "cell_aws_usw2_cur",
            '${replace("${var.cluster_name}_cur", "-", "_")}',
        ]
        for template in cases:
            with self.subTest(template=template):
                errors = validate_cloud_name(template, is_glue=True)
                self.assertEqual(errors, [], f"Template: {template}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            tf_file = repo_root / "src/infra/terraform/glue.tf"
            tf_file.parent.mkdir(parents=True, exist_ok=True)
            tf_file.write_text(
                """
resource "aws_glue_catalog_database" "cur" {
  name = replace("${var.cluster_name}_cur", "-", "_")
}
""",
                encoding="utf-8",
            )
            violations = inspect_terraform_file(tf_file, repo_root)
            self.assertEqual(len(violations), 0)

    def test_deployment_tags_rule(self) -> None:
        """Verify that every deployment declares tags variable, AWS default_tags, and resource-tags annotation."""
        cases = [
            # Rejecting case: missing tags variable
            (
                "missing_tags_variable",
                {
                    "variables.tf": """
variable "cluster_name" {
  type = string
}
""",
                    "versions.tf": """
provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = var.tags
  }
}
""",
                    "main.tf": """
module "control_plane" {
  source = "./ctrl"
  annotations = {
    "resource-tags" = jsonencode(var.tags)
  }
}
""",
                },
                "variable.tags",
                "Deployment",
            ),
            # Rejecting case: tags variable has non-map(string) type
            (
                "invalid_tags_type",
                {
                    "variables.tf": """
variable "tags" {
  type = string
}
""",
                    "versions.tf": """
provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = var.tags
  }
}
""",
                    "main.tf": """
module "control_plane" {
  source = "./ctrl"
  annotations = {
    "resource-tags" = jsonencode(var.tags)
  }
}
""",
                },
                "variable.tags",
                "must be of type map(string)",
            ),
            # Rejecting case: AWS provider missing default_tags
            (
                "missing_provider_default_tags",
                {
                    "variables.tf": """
variable "tags" {
  type = map(string)
}
""",
                    "versions.tf": """
provider "aws" {
  region = "us-west-2"
}
""",
                    "main.tf": """
module "control_plane" {
  source = "./ctrl"
  annotations = {
    "resource-tags" = jsonencode(var.tags)
  }
}
""",
                },
                "provider.aws.default_tags",
                "must configure default_tags block",
            ),
            # Rejecting case: AWS provider default_tags not referencing var.tags
            (
                "hardcoded_provider_default_tags",
                {
                    "variables.tf": """
variable "tags" {
  type = map(string)
}
""",
                    "versions.tf": """
provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = {
      environment = "production"
    }
  }
}
""",
                    "main.tf": """
module "control_plane" {
  source = "./ctrl"
  annotations = {
    "resource-tags" = jsonencode(var.tags)
  }
}
""",
                },
                "provider.aws.default_tags.tags",
                "must set tags = var.tags",
            ),
            # Rejecting case: module control_plane missing resource-tags annotation
            (
                "missing_resource_tags_annotation",
                {
                    "variables.tf": """
variable "tags" {
  type = map(string)
}
""",
                    "versions.tf": """
provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = var.tags
  }
}
""",
                    "main.tf": """
module "control_plane" {
  source = "./ctrl"
  annotations = {
    "control-gateway-ipv4" = "10.0.0.1"
  }
}
""",
                },
                "module.control_plane.annotations[resource-tags]",
                "must include 'resource-tags'",
            ),
            # Rejecting case: resource-tags annotation does not use jsonencode(var.tags)
            (
                "invalid_resource_tags_value",
                {
                    "variables.tf": """
variable "tags" {
  type = map(string)
}
""",
                    "versions.tf": """
provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = var.tags
  }
}
""",
                    "main.tf": """
module "control_plane" {
  source = "./ctrl"
  annotations = {
    "resource-tags" = "static-string"
  }
}
""",
                },
                "module.control_plane.annotations[resource-tags]",
                "must be jsonencode(var.tags)",
            ),
            # Rejecting case: registered_cells missing resource-tags annotation
            (
                "missing_cell_resource_tags_annotation",
                {
                    "variables.tf": """
variable "tags" {
  type = map(string)
}
""",
                    "versions.tf": """
provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = var.tags
  }
}
""",
                    "main.tf": """
module "control_plane" {
  source = "./ctrl"
  annotations = {
    "resource-tags" = jsonencode(var.tags)
  }
  registered_cells = [
    {
      name = "cell-1"
      annotations = {
        "aws-region" = "us-west-2"
      }
    }
  ]
}
""",
                },
                "module.control_plane.registered_cells[cell-1].annotations[resource-tags]",
                "must include 'resource-tags'",
            ),
        ]

        for case_name, files, expected_target, expected_msg_part in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    repo_root = Path(tmp_dir)
                    dep_dir = repo_root / "src/infra/terraform/deployments/test_deployment"
                    dep_dir.mkdir(parents=True, exist_ok=True)
                    for fname, content in files.items():
                        (dep_dir / fname).write_text(content, encoding="utf-8")
                    violations = inspect_deployment(dep_dir, repo_root)
                    targets = {v.target for v in violations}
                    self.assertIn(
                        expected_target,
                        targets,
                        f"Case '{case_name}' expected target {expected_target!r} in {targets}",
                    )
                    matching = [v for v in violations if v.target == expected_target]
                    self.assertTrue(
                        any(expected_msg_part in v.message for v in matching),
                        f"Case '{case_name}' message {matching} did not contain {expected_msg_part!r}",
                    )

    def test_compliant_deployment_passes_tags_rule(self) -> None:
        """Verify that a compliant deployment with tags, provider default_tags, and resource-tags passes without violations."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            dep_dir = repo_root / "src/infra/terraform/deployments/compliant"
            dep_dir.mkdir(parents=True, exist_ok=True)
            (dep_dir / "variables.tf").write_text(
                """
variable "tags" {
  description = "Deployment tags"
  type        = map(string)
}
""",
                encoding="utf-8",
            )
            (dep_dir / "versions.tf").write_text(
                """
provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = var.tags
  }
}
""",
                encoding="utf-8",
            )
            (dep_dir / "main.tf").write_text(
                """
module "control_plane" {
  source = "./ctrl"

  annotations = {
    "control-gateway-ipv4" = "10.0.0.1"
    "resource-tags"        = jsonencode(var.tags)
  }

  registered_cells = [
    {
      name = "cell-aws-usw2"
      annotations = {
        "aws-region"    = "us-west-2"
        "resource-tags" = jsonencode(var.tags)
      }
    }
  ]
}
""",
                encoding="utf-8",
            )
            violations = inspect_deployment(dep_dir, repo_root)
            self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
