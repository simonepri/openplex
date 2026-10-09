# Tests ECR repository creation, KMS encryption keys, and KMS key aliases in the AWS Registry component.

mock_provider "aws" {
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-west-2:123456789012:key/mock-registry-key"
      key_id = "mock-registry-key-id"
    }
  }

  mock_resource "aws_kms_alias" {
    defaults = {
      arn = "arn:aws:kms:us-west-2:123456789012:alias/mock"
      id  = "alias/mock"
    }
  }

  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-west-2:123456789012:repository/mock"
      repository_url = "123456789012.dkr.ecr.us-west-2.amazonaws.com/ctrl-aws-usw2/infrastructure"
    }
  }

  mock_resource "aws_ecr_lifecycle_policy" {
    defaults = {}
  }

  mock_resource "aws_ecr_repository_creation_template" {
    defaults = {}
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }

  mock_data "aws_region" {
    defaults = {
      name = "us-west-2"
    }
  }
}

variables {
  cluster_name = "ctrl-aws-usw2"
  repositories = ["infrastructure"]
}

run "verifies_kms_alias_and_repository_names" {
  command = plan

  assert {
    condition     = aws_kms_alias.registry.name == "alias/ctrl-aws-usw2-registry"
    error_message = "KMS alias must be named alias/<cluster_name>-registry when kms_alias_prefix is empty."
  }

  assert {
    condition     = aws_ecr_repository.this["infrastructure"].name == "ctrl-aws-usw2/infrastructure"
    error_message = "Repository name must remain unchanged as <cluster_name>/infrastructure."
  }

  assert {
    condition     = aws_ecr_repository_creation_template.src.prefix == "src"
    error_message = "Repository creation template prefix must remain unchanged as src."
  }

  assert {
    condition     = aws_kms_key.registry.enable_key_rotation == true
    error_message = "KMS key must have key rotation enabled."
  }
}

run "verifies_kms_alias_with_kms_alias_prefix" {
  command = plan

  variables {
    kms_alias_prefix = "corp-"
  }

  assert {
    condition     = aws_kms_alias.registry.name == "alias/corp-ctrl-aws-usw2-registry"
    error_message = "KMS alias must include kms_alias_prefix."
  }
}
