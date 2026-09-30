# Tests that workspace publisher IAM role OIDC trust policy strictly pins repository and branch without wildcards.

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
  public_domain_name            = "openplex.org"
  git_repo_url                  = "git@github.com:simonepri/openplex.git"
  registered_cells              = []
  disabled_components           = ["karpenter"]
  enable_identity               = false
  enable_dns                    = false
  enable_network_mesh           = false
  enable_cloud_cost             = false
  enable_kms_secrets_encryption = false
  enable_flow_logs              = false
}

run "verifies_publisher_oidc_trust_policy_exact_and_pinned" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.workspace_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "Workspace publisher trust policy must require aud = sts.amazonaws.com via StringEquals."
  }

  assert {
    condition     = jsondecode(aws_iam_role.workspace_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:simonepri/openplex:ref:refs/heads/main"
    error_message = "Workspace publisher trust policy sub must exactly equal repo:simonepri/openplex:ref:refs/heads/main."
  }

  assert {
    condition     = !strcontains(jsondecode(aws_iam_role.workspace_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"], "*")
    error_message = "Workspace publisher trust policy sub must not contain wildcards."
  }

  assert {
    condition     = !can(jsondecode(aws_iam_role.workspace_publisher.assume_role_policy).Statement[0].Condition.StringLike)
    error_message = "Workspace publisher trust policy must not contain StringLike conditions."
  }
}

run "verifies_publisher_oidc_trust_policy_custom_target_revision" {
  command = plan

  variables {
    target_revision = "release-v1"
  }

  assert {
    condition     = jsondecode(aws_iam_role.workspace_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:simonepri/openplex:ref:refs/heads/release-v1"
    error_message = "Workspace publisher trust policy sub must track custom target_revision."
  }

  assert {
    condition     = !strcontains(jsondecode(aws_iam_role.workspace_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"], "*")
    error_message = "Workspace publisher trust policy sub must not contain wildcards with custom target_revision."
  }
}
