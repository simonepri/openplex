# Tests Karpenter interruption handling and S3 gateway storage-stats Pod Identity in the AWS cell topology.

override_module {
  target = module.network_mesh
}

mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }

  mock_resource "aws_iam_policy" {
    defaults = {
      arn = "arn:aws:iam::123456789012:policy/mock"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      arn = "arn:aws:kms:us-east-1:123456789012:key/mock"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn                = "arn:aws:s3:::cell-aws-test-meta-123456789012"
      id                 = "cell-aws-test-meta-123456789012"
      bucket_domain_name = "cell-aws-test-meta-123456789012.s3.us-east-1.amazonaws.com"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock"
    }
  }

  mock_resource "aws_launch_template" {
    defaults = {
      id             = "lt-1234567890abcdef0"
      latest_version = 1
    }
  }

  mock_resource "aws_eks_cluster" {
    defaults = {
      endpoint = "https://mock.eks.us-east-1.amazonaws.com"
      certificate_authority = [{
        data = "dGVzdC1jYQ=="
      }]
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/MOCK"
        }]
      }]
      vpc_config = {
        cluster_security_group_id = "sg-12345678"
      }
    }
  }

  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:123456789012:cell-aws-test-karpenter"
      id  = "https://sqs.us-east-1.amazonaws.com/123456789012/cell-aws-test-karpenter"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }

  mock_data "aws_region" {
    defaults = {
      name   = "us-east-1"
      region = "us-east-1"
    }
  }

  mock_data "aws_vpc" {
    defaults = {
      cidr_block = "10.1.0.0/16"
    }
  }
}

variables {
  cluster_name        = "cell-aws-test"
  vpc_cidr            = "10.1.0.0/16"
  availability_zones  = ["us-east-1a", "us-east-1b"]
  enable_network_mesh = false
  tier_subnets = {
    private = ["10.1.0.0/24", "10.1.1.0/24"]
    public  = ["10.1.10.0/24", "10.1.11.0/24"]
    pod     = []
  }
}

run "verifies_karpenter_interruption_eventbridge_rules_and_targets" {
  command = plan

  assert {
    condition     = module.cluster[0].karpenter_interruption_queue_arn != null
    error_message = "Karpenter interruption queue ARN must be configured in module.cluster."
  }

  assert {
    condition     = module.cluster[0].karpenter_interruption_queue_name == "cell-aws-test-karpenter"
    error_message = "Karpenter interruption queue name must match the cluster name in module.cluster."
  }
}

run "verifies_karpenter_interruption_rules_omitted_when_disabled" {
  command = plan

  variables {
    disabled_components = ["karpenter"]
  }

  assert {
    condition     = module.cluster[0].karpenter_interruption_queue_arn == null
    error_message = "Karpenter interruption queue must not be created when karpenter is disabled."
  }

  assert {
    condition     = module.cluster[0].karpenter_interruption_queue_name == null
    error_message = "Karpenter interruption queue must not be created when karpenter is disabled."
  }
}

run "verifies_storage_stats_pod_identity_role_present" {
  command = plan

  assert {
    condition     = contains(keys(module.identity[0].record.role_arns), "storage-stats")
    error_message = "The storage-stats Pod Identity role must be provisioned when identity is enabled."
  }

  assert {
    condition     = module.identity[0].record.trust_mode == "pod_identity"
    error_message = "The identity component must use Pod Identity trust for the storage-stats role."
  }
}

run "verifies_instance_profile_output" {
  command = plan

  assert {
    condition     = output.karpenter_instance_profile_name == "cell-aws-test-karpenter-node"
    error_message = "karpenter_instance_profile_name output must match cluster Karpenter node profile."
  }

  assert {
    condition     = output.instance_profile_name == "cell-aws-test-karpenter-node"
    error_message = "instance_profile_name output must match cluster Karpenter node profile."
  }
}

run "verifies_team_reader_roles_and_secrets" {
  command = plan

  assert {
    condition     = contains(keys(module.identity[0].record.role_arns), "s3-gateway-examples-reader")
    error_message = "The s3-gateway-examples-reader role must be provisioned in identity component."
  }

  assert {
    condition     = module.s3_team_reader_secrets["examples"].record.secret_name == "cell-aws-test-s3-team-examples-reader"
    error_message = "Secret for team examples reader must be named cell-aws-test-s3-team-examples-reader."
  }

  assert {
    condition     = contains(keys(random_password.caller_secret_access_key), "examples-reader")
    error_message = "Caller secret access key must be generated for examples-reader."
  }
}

run "verifies_workspaces_role_and_secret_removed" {
  command = plan

  assert {
    condition     = !contains(keys(module.identity[0].record.role_arns), "workspaces")
    error_message = "The workspaces role must be removed from identity roles."
  }

  assert {
    condition     = !contains(keys(random_password.caller_secret_access_key), "workspaces")
    error_message = "The caller secret access key must not contain workspaces."
  }
}
