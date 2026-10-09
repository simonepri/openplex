# Tests secret provisioning, KMS encryption keys, and KMS key aliases in the AWS Secret Manager component.

mock_provider "aws" {
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-west-2:123456789012:key/mock-secret-key"
      key_id = "mock-secret-key-id"
    }
  }

  mock_resource "aws_kms_alias" {
    defaults = {
      arn = "arn:aws:kms:us-west-2:123456789012:alias/mock"
      id  = "alias/mock"
    }
  }

  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:mock-secret"
      id  = "arn:aws:secretsmanager:us-west-2:123456789012:secret:mock-secret"
    }
  }

  mock_resource "aws_secretsmanager_secret_version" {
    defaults = {
      id = "mock-version-id"
    }
  }
}

variables {
  secret_name = "test-secret"
  secret_values = {
    key1 = "value1"
    key2 = "value2"
  }
}

run "verifies_kms_alias_and_secret_resources" {
  command = plan

  assert {
    condition     = aws_kms_alias.secret.name == "alias/test-secret"
    error_message = "KMS alias must be named alias/<secret_name> when kms_alias_prefix is empty."
  }

  assert {
    condition     = aws_secretsmanager_secret.secret.name == "test-secret"
    error_message = "Secret name must match secret_name parameter."
  }

  assert {
    condition     = aws_kms_key.secret.enable_key_rotation == true
    error_message = "KMS key must have rotation enabled."
  }

  assert {
    condition     = output.record.secret_name == "test-secret"
    error_message = "Output record must contain secret_name."
  }
}

run "verifies_kms_alias_with_kms_alias_prefix" {
  command = plan

  variables {
    kms_alias_prefix = "corp-"
  }

  assert {
    condition     = aws_kms_alias.secret.name == "alias/corp-test-secret"
    error_message = "KMS alias must include kms_alias_prefix."
  }
}
