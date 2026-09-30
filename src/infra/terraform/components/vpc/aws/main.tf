# Provisions AWS VPC networks, public/private/pod subnets, internet and NAT gateways, and route tables.

module "interface" {
  source             = "../_interface"
  name               = var.name
  cidr_block         = var.cidr_block
  availability_zones = var.availability_zones
  tier_subnets       = var.tier_subnets
  enable_flow_logs   = var.enable_flow_logs
  realized = {
    vpc_id             = aws_vpc.this.id
    private_subnet_ids = [for s in aws_subnet.private : s.id]
    public_subnet_ids  = [for s in aws_subnet.public : s.id]
    pod_subnet_ids     = [for s in aws_subnet.pod : s.id]
    nat_gateway_ips    = [for e in aws_eip.nat : e.public_ip]
  }
}

locals {
  effective_cluster_name = coalesce(var.cluster_name, trimsuffix(var.name, "-vpc"))
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = module.interface.names.vpc
  }
}

resource "aws_subnet" "private" {
  count             = length(coalesce(var.tier_subnets.private, []))
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.tier_subnets.private[count.index]
  availability_zone = length(var.availability_zones) > 0 ? var.availability_zones[count.index % length(var.availability_zones)] : null

  tags = {
    Name                              = module.interface.names.subnets.private[count.index]
    Tier                              = "private"
    "karpenter.sh/discovery"          = local.effective_cluster_name
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_subnet" "public" {
  count                   = length(coalesce(var.tier_subnets.public, []))
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.tier_subnets.public[count.index]
  availability_zone       = length(var.availability_zones) > 0 ? var.availability_zones[count.index % length(var.availability_zones)] : null
  map_public_ip_on_launch = false

  tags = {
    Name                     = module.interface.names.subnets.public[count.index]
    Tier                     = "public"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "pod" {
  count             = length(coalesce(var.tier_subnets.pod, []))
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.tier_subnets.pod[count.index]
  availability_zone = length(var.availability_zones) > 0 ? var.availability_zones[count.index % length(var.availability_zones)] : null

  tags = {
    Name                              = module.interface.names.subnets.pod[count.index]
    Tier                              = "pod"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = module.interface.names.internet_gateway
  }
}

resource "aws_eip" "nat" {
  count  = length(coalesce(var.tier_subnets.public, []))
  domain = "vpc"

  tags = {
    Name = "${module.interface.names.vpc}-nat-${count.index}"
  }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count         = length(coalesce(var.tier_subnets.public, []))
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name = "${module.interface.names.vpc}-nat-${count.index}"
  }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  count  = length(coalesce(var.tier_subnets.public, [])) > 0 ? 1 : 0
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${module.interface.names.vpc}-public"
  }
}

resource "aws_route" "public_internet" {
  count                  = length(coalesce(var.tier_subnets.public, [])) > 0 ? 1 : 0
  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = length(coalesce(var.tier_subnets.public, []))
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_route_table" "private" {
  count  = length(coalesce(var.tier_subnets.private, []))
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${module.interface.names.vpc}-private-${count.index}"
  }
}

resource "aws_route" "private_nat" {
  count                  = length(aws_nat_gateway.this) > 0 ? length(coalesce(var.tier_subnets.private, [])) : 0
  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[count.index % length(aws_nat_gateway.this)].id
}

resource "aws_route_table_association" "private" {
  count          = length(coalesce(var.tier_subnets.private, []))
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table" "pod" {
  count  = length(coalesce(var.tier_subnets.pod, []))
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${module.interface.names.vpc}-pod-${count.index}"
  }
}

resource "aws_route" "pod_nat" {
  count                  = length(aws_nat_gateway.this) > 0 ? length(coalesce(var.tier_subnets.pod, [])) : 0
  route_table_id         = aws_route_table.pod[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[count.index % length(aws_nat_gateway.this)].id
}

resource "aws_route_table_association" "pod" {
  count          = length(coalesce(var.tier_subnets.pod, []))
  subnet_id      = aws_subnet.pod[count.index].id
  route_table_id = aws_route_table.pod[count.index].id
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_kms_key" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  description             = "Customer-managed key for ${module.interface.names.vpc} VPC flow logs"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootPermissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  count             = var.enable_flow_logs ? 1 : 0
  name              = "/aws/vpc/${module.interface.names.vpc}/flow-logs"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.flow_logs[0].arn
}

resource "aws_iam_role" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0
  name  = "${module.interface.names.vpc}-flow-logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0
  name  = "${module.interface.names.vpc}-flow-logs"
  role  = aws_iam_role.flow_logs[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Effect   = "Allow"
        Resource = "*"
      }
    ]
  })
}

resource "aws_flow_log" "this" {
  count           = var.enable_flow_logs ? 1 : 0
  iam_role_arn    = aws_iam_role.flow_logs[0].arn
  log_destination = aws_cloudwatch_log_group.flow_logs[0].arn
  traffic_type    = "ALL"
  vpc_id          = aws_vpc.this.id
}
