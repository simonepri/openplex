# Tests CloudTrail trail, CloudWatch log group, S3 archive bucket, KMS key, and IAM configuration.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      id     = "mock-kms-key-id"
      key_id = "mock-kms-key-id"
      arn    = "arn:aws:kms:us-west-2:123456789012:key/mock-kms-key-id"
    }
  }

  mock_resource "aws_kms_alias" {
    defaults = {
      id  = "alias/cloudtrail-test-cluster"
      arn = "arn:aws:kms:us-west-2:123456789012:alias/cloudtrail-test-cluster"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = {
      id  = "test-cluster-cloudtrail-123456789012"
      arn = "arn:aws:s3:::test-cluster-cloudtrail-123456789012"
    }
  }

  mock_resource "aws_s3_bucket_server_side_encryption_configuration" {}
  mock_resource "aws_s3_bucket_public_access_block" {}
  mock_resource "aws_s3_bucket_versioning" {}
  mock_resource "aws_s3_bucket_lifecycle_configuration" {}
  mock_resource "aws_s3_bucket_policy" {}
  mock_resource "aws_s3_bucket_logging" {}

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      id  = "/aws/cloudtrail/test-cluster"
      arn = "arn:aws:logs:us-west-2:123456789012:log-group:/aws/cloudtrail/test-cluster"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      id  = "test-cluster-cloudtrail-cloudwatch"
      arn = "arn:aws:iam::123456789012:role/test-cluster-cloudtrail-cloudwatch"
    }
  }

  mock_resource "aws_iam_role_policy" {
    defaults = {
      id = "test-cluster-cloudtrail-cloudwatch"
    }
  }

  mock_resource "aws_cloudtrail" {
    defaults = {
      id  = "test-cluster"
      arn = "arn:aws:cloudtrail:us-west-2:123456789012:trail/test-cluster"
    }
  }
}

variables {
  cluster_name              = "test-cluster"
  s3_data_event_bucket_arns = ["arn:aws:s3:::my-bucket", "arn:aws:s3:::trailing-slash/"]
  tags = {
    Environment = "test"
  }
  retention_in_days = 90
}

run "verifies_cloudtrail_plan_and_outputs" {
  command = plan

  assert {
    condition     = aws_cloudtrail.this.name == "test-cluster"
    error_message = "CloudTrail name must match the cluster name."
  }

  assert {
    condition     = aws_cloudtrail.this.is_multi_region_trail == true
    error_message = "CloudTrail must be a multi-region trail."
  }

  assert {
    condition     = aws_cloudtrail.this.enable_log_file_validation == true
    error_message = "CloudTrail log file validation must be enabled."
  }

  assert {
    condition     = aws_cloudtrail.this.include_global_service_events == true
    error_message = "CloudTrail must include global service events."
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.name == "/aws/cloudtrail/test-cluster"
    error_message = "CloudWatch log group name must match /aws/cloudtrail/${var.cluster_name}."
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.retention_in_days == 90
    error_message = "CloudWatch log group retention must match var.retention_in_days."
  }

  assert {
    condition     = output.log_group_arn == "arn:aws:logs:us-west-2:123456789012:log-group:/aws/cloudtrail/test-cluster"
    error_message = "log_group_arn output must match the CloudWatch log group ARN."
  }

  assert {
    condition     = output.log_group_name == "/aws/cloudtrail/test-cluster"
    error_message = "log_group_name output must match the CloudWatch log group name."
  }

  assert {
    condition     = output.trail_arn == "arn:aws:cloudtrail:us-west-2:123456789012:trail/test-cluster"
    error_message = "trail_arn output must match the CloudTrail ARN."
  }

  assert {
    condition     = output.s3_bucket_arn == "arn:aws:s3:::test-cluster-cloudtrail-123456789012"
    error_message = "s3_bucket_arn output must match the S3 bucket ARN."
  }

  assert {
    condition     = output.kms_key_arn == "arn:aws:kms:us-west-2:123456789012:key/mock-kms-key-id"
    error_message = "kms_key_arn output must match the KMS key ARN."
  }

  assert {
    condition     = aws_s3_bucket_logging.this.target_bucket == aws_s3_bucket.access_logs.id
    error_message = "S3 bucket access logging must be enabled on the CloudTrail S3 bucket targeting access_logs."
  }
}

run "records_all_management_events_and_only_s3_writes" {
  command = plan

  assert {
    condition = length([
      for fs in one([for sel in aws_cloudtrail.this.advanced_event_selector : sel if sel.name == "Management events"]).field_selector : fs
      if fs.field == "readOnly"
    ]) == 0
    error_message = "The management selector must not filter readOnly, so it records reads and writes."
  }

  assert {
    condition = one([
      for fs in one([for sel in aws_cloudtrail.this.advanced_event_selector : sel if sel.name == "S3 data events"]).field_selector : fs.equals
      if fs.field == "readOnly"
    ]) == tolist(["false"])
    error_message = "The S3 data selector must record writes and deletes only."
  }
}

run "verifies_without_s3_data_events" {
  command = plan

  variables {
    cluster_name              = "test-cluster-no-data"
    s3_data_event_bucket_arns = []
    tags                      = {}
    retention_in_days         = 30
  }

  assert {
    condition     = aws_cloudtrail.this.name == "test-cluster-no-data"
    error_message = "CloudTrail name must match the cluster name."
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.name == "/aws/cloudtrail/test-cluster-no-data"
    error_message = "CloudWatch log group name must match /aws/cloudtrail/${var.cluster_name}."
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.retention_in_days == 30
    error_message = "CloudWatch log group retention must match var.retention_in_days."
  }
}
