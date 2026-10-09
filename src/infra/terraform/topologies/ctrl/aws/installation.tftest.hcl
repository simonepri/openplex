# Tests that cluster annotations publish tags and account ID without installation prefix.

mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
      id  = "mock"
    }
  }

  mock_resource "aws_iam_role_policy" {
    defaults = {
      id = "mock"
    }
  }

  mock_resource "aws_iam_policy" {
    defaults = {
      arn = "arn:aws:iam::123456789012:policy/mock"
    }
  }

  mock_resource "aws_iam_openid_connect_provider" {
    defaults = {
      arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::mock"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      arn = "arn:aws:kms:us-west-2:123456789012:key/mock"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-west-2:123456789012:log-group:mock"
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
      endpoint = "https://mock.eks.us-west-2.amazonaws.com"
      certificate_authority = [{
        data = "dGVzdC1jYQ=="
      }]
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.us-west-2.amazonaws.com/id/MOCK"
        }]
      }]
      vpc_config = {
        cluster_security_group_id = "sg-12345678"
      }
    }
  }

  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-west-2:123456789012:ctrl-aws-usw2-karpenter"
      id  = "https://sqs.us-west-2.amazonaws.com/123456789012/ctrl-aws-usw2-karpenter"
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
      name = "us-west-2"
    }
  }

  mock_data "aws_vpc" {
    defaults = {
      cidr_block = "10.0.0.0/16"
    }
  }
}

mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "tailscale" {}

variables {
  cluster_name       = "ctrl-aws-usw2"
  vpc_cidr           = "10.0.0.0/16"
  availability_zones = ["us-west-2a", "us-west-2b"]
  tier_subnets = {
    private = ["10.0.1.0/24", "10.0.2.0/24"]
    public  = ["10.0.10.0/24", "10.0.11.0/24"]
    pod     = []
  }
  cell_service_cidrs            = ["172.20.0.0/20"]
  public_domain_name            = "example.com"
  git_repo_url                  = "git@github.com:openplex/openplex.git"
  registered_cells              = []
  disabled_components           = ["karpenter"]
  enable_identity               = false
  enable_dns                    = false
  enable_network_mesh           = false
  enable_cloud_cost             = false
  enable_kms_secrets_encryption = false
  enable_flow_logs              = false
}

run "verifies_resource_tags_and_account_id_annotations" {
  command = plan

  variables {
    account_id = "123456789012"
    tags = {
      environment = "research"
    }
  }

  assert {
    condition     = output.cluster_annotations["resource-tags"] == jsonencode({ environment = "research" })
    error_message = "Resource tags cluster annotation must match encoded tags."
  }

  assert {
    condition     = output.cluster_annotations["aws-account-id"] == "123456789012"
    error_message = "AWS account ID cluster annotation must match account_id."
  }

  assert {
    condition     = !contains(keys(output.cluster_annotations), "installation")
    error_message = "Installation cluster annotation must not be present."
  }
}

run "publishes_opencost_cloud_cost_gate_when_enabled" {
  command = plan

  variables {
    enable_cloud_cost = true
  }

  assert {
    condition     = output.cluster_annotations["opencost-cloud-cost-reader"] == "true"
    error_message = "Cloud cost reader annotation must be true when enable_cloud_cost is true."
  }
}

run "withholds_opencost_cloud_cost_gate_when_disabled" {
  command = plan

  assert {
    condition     = output.cluster_annotations["opencost-cloud-cost-reader"] == "false"
    error_message = "Cloud cost reader annotation must be false when enable_cloud_cost is false."
  }
}

run "verifies_workspace_snapshot_root_secret" {
  command = plan

  assert {
    condition     = module.workspace_snapshot_root_secret[0].record.secret_name == "ctrl-aws-usw2-workspace-snapshot-root"
    error_message = "Workspace snapshot root secret name must follow <cluster>-workspace-snapshot-root."
  }

  assert {
    condition     = contains(module.workspace_snapshot_root_secret[0].record.keys, "root_key")
    error_message = "Workspace snapshot root secret must contain root_key."
  }
}
