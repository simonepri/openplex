# Tests that Renovate ECR read IAM user and policy are strictly restricted to reading image digests without layer download or write access.

mock_provider "aws" {
  mock_resource "aws_iam_user" {
    defaults = {
      arn = "arn:aws:iam::123456789012:user/ctrl-aws-usw2-renovate-ecr-read"
      id  = "ctrl-aws-usw2-renovate-ecr-read"
    }
  }

  mock_resource "aws_iam_user_policy" {
    defaults = {
      id = "ctrl-aws-usw2-renovate-ecr-read-policy"
    }
  }

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

run "verifies_renovate_ecr_read_user_policy_least_privilege" {
  command = plan

  assert {
    condition     = aws_iam_user.renovate_ecr_read.name == "ctrl-aws-usw2-renovate-ecr-read"
    error_message = "Renovate ECR read IAM user name must equal ctrl-aws-usw2-renovate-ecr-read."
  }

  assert {
    condition     = aws_iam_user_policy.renovate_ecr_read.user == aws_iam_user.renovate_ecr_read.name
    error_message = "Renovate ECR read IAM user policy must attach to the renovate ECR read user."
  }

  assert {
    condition     = jsondecode(aws_iam_user_policy.renovate_ecr_read.policy).Statement[0].Action == ["ecr:GetAuthorizationToken"]
    error_message = "Renovate ECR read policy must allow ecr:GetAuthorizationToken on *."
  }

  assert {
    condition     = jsondecode(aws_iam_user_policy.renovate_ecr_read.policy).Statement[0].Resource == "*"
    error_message = "Renovate ECR authorization token statement must target *."
  }

  assert {
    condition     = contains(jsondecode(aws_iam_user_policy.renovate_ecr_read.policy).Statement[1].Action, "ecr:BatchGetImage") && contains(jsondecode(aws_iam_user_policy.renovate_ecr_read.policy).Statement[1].Action, "ecr:ListImages")
    error_message = "Renovate ECR read policy statement must allow ecr:BatchGetImage and ecr:ListImages."
  }

  assert {
    condition     = can(regex(":repository/src/\\*$", jsondecode(aws_iam_user_policy.renovate_ecr_read.policy).Statement[1].Resource[0]))
    error_message = "Renovate ECR read policy repository resource must match repository/src/*."
  }

  assert {
    condition     = !strcontains(aws_iam_user_policy.renovate_ecr_read.policy, "GetDownloadUrlForLayer")
    error_message = "Renovate ECR read policy must not grant ecr:GetDownloadUrlForLayer."
  }

  assert {
    condition     = !strcontains(aws_iam_user_policy.renovate_ecr_read.policy, "PutImage") && !strcontains(aws_iam_user_policy.renovate_ecr_read.policy, "Upload") && !strcontains(aws_iam_user_policy.renovate_ecr_read.policy, "Create") && !strcontains(aws_iam_user_policy.renovate_ecr_read.policy, "Delete") && !strcontains(aws_iam_user_policy.renovate_ecr_read.policy, "Tag")
    error_message = "Renovate ECR read policy must not allow write or mutating actions."
  }

  assert {
    condition     = length(setsubtract(flatten([for s in jsondecode(aws_iam_user_policy.renovate_ecr_read.policy).Statement : s.Action]), ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:ListImages"])) == 0
    error_message = "Renovate ECR read policy must not contain any actions other than ecr:GetAuthorizationToken, ecr:BatchGetImage, and ecr:ListImages."
  }
}

run "verifies_renovate_ecr_read_user_with_iam_name_prefix" {
  command = plan

  variables {
    iam_name_prefix = "compliance-"
  }

  assert {
    condition     = aws_iam_user.renovate_ecr_read.name == "compliance-ctrl-aws-usw2-renovate-ecr-read"
    error_message = "Renovate ECR read IAM user name must include iam_name_prefix."
  }

  assert {
    condition     = aws_iam_user_policy.renovate_ecr_read.name == "compliance-ctrl-aws-usw2-renovate-ecr-read-policy"
    error_message = "Renovate ECR read IAM user policy name must include iam_name_prefix."
  }
}
