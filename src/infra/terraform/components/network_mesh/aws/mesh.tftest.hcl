# Tests IAM naming, permissions boundary, KMS aliases, and Tailscale secret naming for the network mesh router component.

mock_provider "aws" {
  mock_resource "aws_security_group" {
    defaults = {
      id  = "sg-12345678"
      arn = "arn:aws:ec2:us-west-2:123456789012:security-group/sg-12345678"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      id     = "key-12345678"
      key_id = "key-12345678"
      arn    = "arn:aws:kms:us-west-2:123456789012:key/key-12345678"
    }
  }

  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      id  = "arn:aws:secretsmanager:us-west-2:123456789012:secret:mock"
      arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:mock"
    }
  }

  mock_resource "aws_secretsmanager_secret_version" {
    defaults = {
      id = "mock-version-id"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      id  = "mock-role-id"
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }

  mock_resource "aws_iam_role_policy" {
    defaults = {
      id = "mock-policy-id"
    }
  }

  mock_resource "aws_iam_instance_profile" {
    defaults = {
      id  = "mock-profile-id"
      arn = "arn:aws:iam::123456789012:instance-profile/mock"
    }
  }

  mock_resource "aws_instance" {
    defaults = {
      id                           = "i-1234567890abcdef0"
      primary_network_interface_id = "eni-12345678"
      private_ip                   = "10.0.1.50"
    }
  }

  mock_data "aws_region" {
    defaults = {
      name   = "us-west-2"
      region = "us-west-2"
    }
  }

  mock_data "aws_ami" {
    defaults = {
      id = "ami-12345678"
    }
  }
}

mock_provider "tailscale" {
  mock_resource "tailscale_oauth_client" {
    defaults = {
      id  = "mock-oauth-client-id"
      key = "mock-oauth-client-key"
    }
  }
}

variables {
  name                     = "test-cluster"
  cluster_name             = "test-cluster"
  vpc_id                   = "vpc-12345678"
  subnet_id                = "subnet-12345678"
  tailnet_auth_key         = "tskey-auth-mock"
  iam_name_prefix          = "pfx-"
  kms_alias_prefix         = "kms-"
  iam_permissions_boundary = "arn:aws:iam::123456789012:policy/boundary"
  enable_k8s_operator      = true
}

run "verifies_mesh_router_iam_and_kms_compliance" {
  command = plan

  assert {
    condition     = aws_iam_role.router.name == "pfx-test-cluster-mesh-router"
    error_message = "Router role name must be <prefix><cluster>-mesh-router without duplicate words."
  }

  assert {
    condition     = aws_iam_instance_profile.router.name == "pfx-test-cluster-mesh-router"
    error_message = "Router instance profile name must match <prefix><cluster>-mesh-router."
  }

  assert {
    condition     = aws_iam_role.router.permissions_boundary == "arn:aws:iam::123456789012:policy/boundary"
    error_message = "Router role must attach the permissions boundary."
  }

  assert {
    condition     = aws_iam_role_policy.router.name == "pfx-test-cluster-mesh-router-secret"
    error_message = "Router role policy must use the prefixed compliant name."
  }

  assert {
    condition     = aws_secretsmanager_secret.tailscale_auth_key.name == "test-cluster-mesh-router-tailscale-auth-key"
    error_message = "Tailscale auth key secret name must remain unchanged."
  }

  assert {
    condition     = aws_kms_alias.tailscale_auth_key.name == "alias/kms-test-cluster-tailscale-auth-key"
    error_message = "Tailscale auth key KMS alias must follow alias/<kms_alias_prefix><cluster>-tailscale-auth-key."
  }

  assert {
    condition     = aws_kms_alias.operator_oauth[0].name == "alias/kms-test-cluster-operator-oauth"
    error_message = "Operator OAuth KMS alias must follow alias/<kms_alias_prefix><cluster>-operator-oauth."
  }
}
