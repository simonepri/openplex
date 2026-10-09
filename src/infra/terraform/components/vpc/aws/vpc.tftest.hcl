# Tests IAM naming, permissions boundary, and KMS aliases for the VPC component.

mock_provider "aws" {
  mock_resource "aws_vpc" {
    defaults = {
      id  = "vpc-12345678"
      arn = "arn:aws:ec2:us-west-2:123456789012:vpc/vpc-12345678"
    }
  }

  mock_resource "aws_subnet" {
    defaults = {
      id  = "subnet-12345678"
      arn = "arn:aws:ec2:us-west-2:123456789012:subnet/subnet-12345678"
    }
  }

  mock_resource "aws_internet_gateway" {
    defaults = {
      id = "igw-12345678"
    }
  }

  mock_resource "aws_eip" {
    defaults = {
      id        = "eipalloc-12345678"
      public_ip = "198.51.100.1"
    }
  }

  mock_resource "aws_nat_gateway" {
    defaults = {
      id = "nat-12345678"
    }
  }

  mock_resource "aws_route_table" {
    defaults = {
      id = "rtb-12345678"
    }
  }

  mock_resource "aws_route" {
    defaults = {
      id = "r-rtb-12345678"
    }
  }

  mock_resource "aws_route_table_association" {
    defaults = {
      id = "rtbassoc-12345678"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      id     = "key-12345678"
      key_id = "key-12345678"
      arn    = "arn:aws:kms:us-west-2:123456789012:key/key-12345678"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      id  = "log-group-mock"
      arn = "arn:aws:logs:us-west-2:123456789012:log-group:mock"
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

  mock_resource "aws_flow_log" {
    defaults = {
      id = "fl-12345678"
    }
  }

  mock_resource "aws_route53_resolver_query_log_config" {
    defaults = {
      id  = "rqlc-12345678"
      arn = "arn:aws:route53resolver:us-west-2:123456789012:resolver-query-log-config/rqlc-12345678"
    }
  }

  mock_resource "aws_route53_resolver_query_log_config_association" {
    defaults = {
      id = "rqlca-12345678"
    }
  }

  mock_data "aws_region" {
    defaults = {
      name = "us-west-2"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }
}

variables {
  name                     = "test-cluster-vpc"
  cidr_block               = "10.0.0.0/16"
  availability_zones       = ["us-west-2a", "us-west-2b"]
  cluster_name             = "test-cluster"
  iam_name_prefix          = "pfx-"
  kms_alias_prefix         = "kms-"
  iam_permissions_boundary = "arn:aws:iam::123456789012:policy/boundary"
  enable_flow_logs         = true
  tier_subnets = {
    private = ["10.0.1.0/24"]
    public  = ["10.0.10.0/24"]
    pod     = []
  }
}

run "verifies_flow_logs_iam_and_kms_compliance" {
  command = plan

  assert {
    condition     = aws_iam_role.flow_logs[0].name == "pfx-test-cluster-vpc-flow-logs"
    error_message = "Flow logs IAM role name must prepend iam_name_prefix."
  }

  assert {
    condition     = aws_iam_role.flow_logs[0].permissions_boundary == "arn:aws:iam::123456789012:policy/boundary"
    error_message = "Flow logs IAM role must attach the permissions boundary."
  }

  assert {
    condition     = aws_iam_role_policy.flow_logs[0].name == "pfx-test-cluster-vpc-flow-logs"
    error_message = "Flow logs IAM role policy name must prepend iam_name_prefix."
  }

  assert {
    condition     = aws_kms_alias.flow_logs[0].name == "alias/kms-test-cluster-flow-logs"
    error_message = "Flow logs KMS alias must follow alias/<kms_alias_prefix><cluster>-flow-logs."
  }

  assert {
    condition     = aws_cloudwatch_log_group.resolver_queries[0].name == "/aws/route53/resolver-queries/test-cluster-vpc"
    error_message = "Resolver queries log group name must match /aws/route53/resolver-queries/<name>."
  }

  assert {
    condition     = aws_cloudwatch_log_group.resolver_queries[0].retention_in_days == 14
    error_message = "Resolver queries log group retention must default to 14 days."
  }

  assert {
    condition     = aws_route53_resolver_query_log_config.this[0].name == "test-cluster-vpc-resolver-queries"
    error_message = "Resolver query log config name must match <name>-resolver-queries."
  }

  assert {
    condition     = aws_route53_resolver_query_log_config.this[0].destination_arn == aws_cloudwatch_log_group.resolver_queries[0].arn
    error_message = "Resolver query log config destination must point to resolver queries log group."
  }

  assert {
    condition     = aws_route53_resolver_query_log_config_association.this[0].resource_id == aws_vpc.this.id
    error_message = "Resolver query log association must link to the VPC."
  }

  assert {
    condition     = output.resolver_query_log_group_name == "/aws/route53/resolver-queries/test-cluster-vpc"
    error_message = "Output resolver_query_log_group_name must match log group name."
  }

  assert {
    condition     = output.resolver_query_log_group_arn == aws_cloudwatch_log_group.resolver_queries[0].arn
    error_message = "Output resolver_query_log_group_arn must match log group arn."
  }
}

run "verifies_resolver_query_logging_disabled" {
  command = plan

  variables {
    enable_resolver_query_logging = false
  }

  assert {
    condition     = length(aws_cloudwatch_log_group.resolver_queries) == 0
    error_message = "Resolver query log group must not be created when disabled."
  }

  assert {
    condition     = length(aws_route53_resolver_query_log_config.this) == 0
    error_message = "Resolver query log config must not be created when disabled."
  }

  assert {
    condition     = length(aws_route53_resolver_query_log_config_association.this) == 0
    error_message = "Resolver query log association must not be created when disabled."
  }

  assert {
    condition     = output.resolver_query_log_group_arn == null
    error_message = "Output resolver_query_log_group_arn must be null when disabled."
  }

  assert {
    condition     = output.resolver_query_log_group_name == null
    error_message = "Output resolver_query_log_group_name must be null when disabled."
  }
}

