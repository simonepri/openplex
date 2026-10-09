# Tests CUR bucket naming, KMS alias, and IAM compliance settings in the AWS cloud cost component.

mock_provider "aws" {
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-west-2:123456789012:key/mock-cur-key"
      key_id = "mock-cur-key"
    }
  }

  mock_resource "aws_kms_alias" {}

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn                = "arn:aws:s3:::mock-cur-bucket"
      bucket_domain_name = "mock-cur-bucket.s3.amazonaws.com"
      id                 = "mock-cur-bucket"
    }
  }

  mock_resource "aws_s3_bucket_server_side_encryption_configuration" {}
  mock_resource "aws_s3_bucket_versioning" {}
  mock_resource "aws_s3_bucket_public_access_block" {}
  mock_resource "aws_s3_bucket_logging" {}
  mock_resource "aws_s3_bucket_policy" {}
  mock_resource "aws_cur_report_definition" {}

  mock_resource "aws_athena_workgroup" {
    defaults = {
      arn = "arn:aws:athena:us-west-2:123456789012:workgroup/mock"
    }
  }

  mock_resource "aws_glue_catalog_database" {
    defaults = {
      arn = "arn:aws:glue:us-west-2:123456789012:database/mock"
    }
  }

  mock_resource "aws_glue_catalog_table" {
    defaults = {
      arn = "arn:aws:glue:us-west-2:123456789012:table/mock"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
      id  = "mock"
    }
  }

  mock_resource "aws_eks_pod_identity_association" {}
  mock_resource "aws_iam_role_policy" {}
  mock_resource "aws_iam_role_policy_attachment" {}
  mock_resource "aws_glue_crawler" {}

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }
}

variables {
  cluster_name             = "ctrl-aws-usw2"
  account_id               = "123456789012"
  cluster_oidc_issuer_url  = "https://oidc.eks.us-west-2.amazonaws.com/id/MOCK"
  cluster_oidc_arn         = "arn:aws:iam::123456789012:oidc-provider/mock"
  iam_name_prefix          = ""
  kms_alias_prefix         = ""
  iam_permissions_boundary = null
}

run "verifies_bucket_naming_kms_alias_and_roles" {
  command = plan

  assert {
    condition     = aws_s3_bucket.cur.bucket == "ctrl-aws-usw2-billing-reports-123456789012"
    error_message = "Billing reports bucket name must follow <cluster>-billing-reports-<account_id> pattern."
  }

  assert {
    condition     = aws_s3_bucket.cur_access_logs.bucket == "ctrl-aws-usw2-billing-access-logs-123456789012"
    error_message = "Billing access logs bucket name must follow <cluster>-billing-access-logs-<account_id> pattern."
  }

  assert {
    condition     = aws_s3_bucket_logging.cur.target_bucket == aws_s3_bucket.cur_access_logs.id
    error_message = "Billing reports bucket must log to the billing access logs bucket."
  }

  assert {
    condition     = aws_s3_bucket_logging.cur.target_prefix == "s3-access-logs/cur/"
    error_message = "Billing reports bucket logging must use the s3-access-logs/cur/ prefix."
  }

  assert {
    condition     = aws_kms_alias.cur.name == "alias/ctrl-aws-usw2-billing"
    error_message = "CUR KMS alias must follow alias/<cluster>-billing pattern."
  }

  assert {
    condition     = aws_iam_role.opencost.name == "ctrl-aws-usw2-opencost"
    error_message = "OpenCost IAM role name must match <cluster>-opencost."
  }

  assert {
    condition     = aws_iam_role.crawler.name == "ctrl-aws-usw2-cur-crawler"
    error_message = "Crawler IAM role name must match <cluster>-cur-crawler."
  }

  assert {
    condition     = aws_glue_catalog_database.cur.name == "ctrl_aws_usw2_cur"
    error_message = "Glue catalog database name must remain with underscores."
  }

  assert {
    condition     = aws_glue_catalog_table.cur.name == "ctrl_aws_usw2_cur"
    error_message = "Glue catalog table name must match <cluster>_cur with underscores."
  }

  assert {
    condition     = aws_glue_catalog_table.cur.storage_descriptor[0].location == "s3://${aws_s3_bucket.cur.id}/cur/ctrl-aws-usw2-cur/ctrl-aws-usw2-cur/"
    error_message = "Glue catalog table location must match the S3 CUR data prefix."
  }

  assert {
    condition     = [for pk in aws_glue_catalog_table.cur.partition_keys : pk.name] == ["year", "month"]
    error_message = "Glue catalog table must have year and month partition keys."
  }

  assert {
    condition     = aws_glue_catalog_table.cur.parameters["compressionType"] == "none" && aws_glue_catalog_table.cur.parameters["classification"] == "parquet"
    error_message = "The CUR table must carry the classification parameters the crawler detects, or the crawler skips it."
  }

  assert {
    condition     = aws_glue_crawler.cur.catalog_target[0].database_name == "ctrl_aws_usw2_cur" && aws_glue_crawler.cur.catalog_target[0].tables[0] == "ctrl_aws_usw2_cur"
    error_message = "Glue crawler must target the catalog table ctrl_aws_usw2_cur."
  }

  assert {
    condition     = aws_glue_crawler.cur.schema_change_policy[0].update_behavior == "UPDATE_IN_DATABASE" && aws_glue_crawler.cur.schema_change_policy[0].delete_behavior == "LOG"
    error_message = "Crawler schema change policy must update in database and log deletes."
  }

  assert {
    condition     = jsondecode(aws_glue_crawler.cur.configuration).CrawlerOutput.Partitions.AddOrUpdateBehavior == "InheritFromTable"
    error_message = "Crawler configuration must specify InheritFromTable for partitions."
  }
}

run "verifies_iam_compliance_prefix_and_boundary" {
  command = plan

  variables {
    iam_name_prefix          = "corp-"
    kms_alias_prefix         = "corp-kms-"
    iam_permissions_boundary = "arn:aws:iam::123456789012:policy/boundary"
  }

  assert {
    condition     = aws_kms_alias.cur.name == "alias/corp-kms-ctrl-aws-usw2-billing"
    error_message = "CUR KMS alias must prepend kms_alias_prefix."
  }

  assert {
    condition     = aws_iam_role.opencost.name == "corp-ctrl-aws-usw2-opencost"
    error_message = "OpenCost IAM role must prepend iam_name_prefix."
  }

  assert {
    condition     = aws_iam_role.crawler.name == "corp-ctrl-aws-usw2-cur-crawler"
    error_message = "Crawler IAM role must prepend iam_name_prefix."
  }

  assert {
    condition     = aws_iam_role.opencost.permissions_boundary == "arn:aws:iam::123456789012:policy/boundary"
    error_message = "OpenCost IAM role must attach permissions boundary."
  }

  assert {
    condition     = aws_iam_role.crawler.permissions_boundary == "arn:aws:iam::123456789012:policy/boundary"
    error_message = "Crawler IAM role must attach permissions boundary."
  }
}
