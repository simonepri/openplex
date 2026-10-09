# Tests S3 bucket naming, KMS encryption, and tier configurations in the AWS storage component.

mock_provider "aws" {
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn                = "arn:aws:s3:::mock-bucket"
      bucket_domain_name = "mock-bucket.s3.amazonaws.com"
      id                 = "mock-bucket"
    }
  }

  mock_resource "aws_s3_bucket_server_side_encryption_configuration" {}
  mock_resource "aws_s3_bucket_logging" {}
  mock_resource "aws_s3_bucket_versioning" {}
  mock_resource "aws_s3_bucket_public_access_block" {}
  mock_resource "aws_s3_bucket_policy" {}
  mock_resource "aws_s3_bucket_inventory" {}
  mock_resource "aws_s3_bucket_lifecycle_configuration" {}

  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-west-2:123456789012:key/mock-storage-key"
      key_id = "mock-storage-key"
    }
  }

  mock_resource "aws_kms_alias" {}

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }
}

variables {
  cluster_name     = "cell-aws-usw2"
  account_id       = "123456789012"
  kms_alias_prefix = ""
}

run "verifies_bucket_naming_and_kms_alias" {
  command = plan

  assert {
    condition     = contains(aws_s3_bucket_inventory.this["home"].optional_fields, "StorageClass")
    error_message = "S3 inventory reports must include StorageClass for s3i queries."
  }

  assert {
    condition     = aws_s3_bucket.this["home"].bucket == "cell-aws-usw2-home-123456789012"
    error_message = "Home bucket name must follow <cluster>-<tier>-<account_id> pattern."
  }

  assert {
    condition     = aws_s3_bucket.this["meta"].bucket == "cell-aws-usw2-meta-123456789012"
    error_message = "Meta bucket name must follow <cluster>-<tier>-<account_id> pattern."
  }

  assert {
    condition     = aws_s3_bucket.this["scratch"].bucket == "cell-aws-usw2-scratch-123456789012"
    error_message = "Scratch bucket name must follow <cluster>-<tier>-<account_id> pattern."
  }

  assert {
    condition     = aws_kms_alias.storage.name == "alias/cell-aws-usw2-storage"
    error_message = "Storage KMS alias must follow alias/<cluster>-storage pattern."
  }

  assert {
    condition     = !contains(keys(aws_s3_bucket_logging.this), "logs")
    error_message = "The 'logs' bucket must not configure S3 access logging to itself."
  }

  assert {
    condition     = contains(keys(aws_s3_bucket_logging.this), "backups")
    error_message = "The 'backups' tier must configure S3 access logging to the logs bucket."
  }
}

run "verifies_kms_alias_prefix_on_kms_alias" {
  command = plan

  variables {
    kms_alias_prefix = "corp-"
  }

  assert {
    condition     = aws_kms_alias.storage.name == "alias/corp-cell-aws-usw2-storage"
    error_message = "Storage KMS alias must prepend kms_alias_prefix."
  }
}
