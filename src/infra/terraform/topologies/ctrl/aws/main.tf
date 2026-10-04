# Composes AWS control plane infrastructure assembling VPC, EKS, ECR registry, and Route 53 DNS.

locals {
  enable_karpenter = !contains(var.disabled_components, "karpenter")
  github_repo_slug = try(regex("(?:github\\.com[:/])([^/]+/[^/.]+?)(?:\\.git)?$", var.git_repo_url)[0], "simonepri/openplex")
  publisher_branch = var.target_revision == "HEAD" ? "main" : var.target_revision
}

module "interface" {
  source = "../_interface"

  cluster_name                  = var.cluster_name
  vpc_cidr                      = var.vpc_cidr
  availability_zones            = var.availability_zones
  tier_subnets                  = var.tier_subnets
  kubernetes_version            = var.kubernetes_version
  service_ipv4_cidr             = var.service_ipv4_cidr
  enable_control_plane_logging  = var.enable_control_plane_logging
  enable_identity               = var.enable_identity
  enable_dns                    = var.enable_dns
  enable_network_mesh           = var.enable_network_mesh
  enable_cloud_cost             = var.enable_cloud_cost
  domain_name                   = var.domain_name
  intranet_domain_name          = var.intranet_domain_name
  public_domain_name            = var.public_domain_name
  cluster_domain_name           = var.cluster_domain_name
  tailnet_auth_key              = var.tailnet_auth_key
  git_repo_url                  = var.git_repo_url
  target_revision               = var.target_revision
  registered_cells              = var.registered_cells
  fleet_availability            = var.fleet_availability
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_flow_logs              = var.enable_flow_logs
  disabled_components           = var.disabled_components
  access_domain_name            = var.access_domain_name
  oidc_tls_insecure_skip_verify = var.oidc_tls_insecure_skip_verify
  annotations                   = var.annotations
  profiles_retention_days       = var.profiles_retention_days

  realized = {
    cluster_name           = module.cluster.record.cluster_name
    cluster_endpoint       = module.cluster.record.endpoint
    cluster_ca_certificate = module.cluster.record.ca_certificate
    vpc_id                 = module.vpc.record.vpc_id
    private_subnet_ids     = module.vpc.record.private_subnet_ids
    public_subnet_ids      = module.vpc.record.public_subnet_ids
    registry_url           = module.registry.record.registry_url
    zone_id                = try(module.dns[0].record.zone_id, null)
    cloud_cost             = try(module.cloud_cost[0].record, null)
    argo_bootstrap         = module.argo_bootstrap.record
    profiles_bucket        = module.storage.record.buckets.profiles.name
  }
}

module "vpc" {
  source = "../../../components/vpc/aws"

  name               = "${var.cluster_name}-vpc"
  cidr_block         = var.vpc_cidr
  availability_zones = var.availability_zones
  tier_subnets       = var.tier_subnets
  enable_flow_logs   = var.enable_flow_logs
  cluster_name       = var.cluster_name
}

module "cluster" {
  source = "../../../components/cluster/aws"

  cluster_name                  = var.cluster_name
  vpc_id                        = module.vpc.record.vpc_id
  subnet_ids                    = module.vpc.record.private_subnet_ids
  pod_subnet_ids                = module.vpc.record.pod_subnet_ids
  kubernetes_version            = var.kubernetes_version
  service_ipv4_cidr             = var.service_ipv4_cidr
  public_access_cidrs           = var.public_access_cidrs
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_control_plane_logging  = var.enable_control_plane_logging
  enable_access_config          = var.enable_access_config
  enable_addons                 = var.enable_addons
  enable_ebs_csi                = var.enable_ebs_csi
  system_instance_types         = var.system_instance_types
  system_desired_size           = var.system_desired_size
  system_max_size               = var.system_max_size
  system_node_taints            = var.system_node_taints
  node_repair_enabled           = var.node_repair_enabled
}

module "storage" {
  source = "../../../components/storage/aws"

  installation_name       = var.resource_prefix != null && var.resource_prefix != "" ? var.resource_prefix : "cloud"
  cell_name               = var.cluster_name
  storage_tiers           = ["home", "scratch", "archive", "backups", "meta", "logs", "profiles"]
  profiles_retention_days = var.profiles_retention_days
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

module "registry" {
  source = "../../../components/registry/aws"

  installation_name = var.cluster_name
  repositories      = ["infrastructure"]
}

module "identity" {
  count  = var.enable_identity ? 1 : 0
  source = "../../../components/identity/aws"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = module.cluster.record.oidc_issuer_url
  cluster_oidc_arn        = module.cluster.record.oidc_provider_arn
  shared_secret_names     = var.shared_secret_names
  storage_kms_key_arn     = module.storage.kms_key_arn

  roles = merge(
    {
      # keep-sorted start block=yes
      atlantis = {
        namespace       = "atlantis"
        service_account = "atlantis"
      }
      aws_load_balancer_controller = {
        namespace       = "kube-system"
        service_account = "aws-load-balancer-controller"
      }
      barman = {
        namespace       = "coder"
        service_account = "coder-postgres"
      }
      buildbuddy_barman = {
        namespace       = "buildbuddy"
        service_account = "buildbuddy-postgres"
      }
      cert_manager = {
        namespace       = "cert-manager-system"
        service_account = "cert-manager"
      }
      cloud_telemetry = {
        namespace       = "otel-system"
        service_account = "otel-collector"
      }
      dragonfly_barman = {
        namespace       = "dragonfly-system"
        service_account = "dragonfly-postgres"
      }
      external_dns = {
        namespace       = "external-dns-system"
        service_account = "external-dns"
      }
      external_secrets = {
        namespace       = "external-secrets-system"
        service_account = "external-secrets"
      }
      parca = {
        namespace       = "parca"
        service_account = "parca"
      }
      prowler = {
        namespace       = "prowler"
        service_account = "prowler"
      }
      signoz_barman = {
        namespace       = "signoz"
        service_account = "signoz-postgres"
      }
      velero = {
        namespace       = "velero-system"
        service_account = "velero-server"
      }
      # keep-sorted end
    },
    local.enable_karpenter ? {
      karpenter = {
        namespace       = "karpenter-system"
        service_account = "karpenter"
      }
    } : {}
  )
}

module "cloud_cost" {
  count  = var.enable_cloud_cost ? 1 : 0
  source = "../../../components/cloud_cost/aws"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = module.cluster.record.oidc_issuer_url
  cluster_oidc_arn        = module.cluster.record.oidc_provider_arn
}


module "dns" {
  count  = var.enable_dns ? 1 : 0
  source = "../../../components/dns/aws"

  domain_name = var.domain_name
  is_cell     = false
}

module "network_mesh" {
  count  = var.enable_network_mesh ? 1 : 0
  source = "../../../components/network_mesh/aws"

  name                = var.cluster_name
  vpc_id              = module.vpc.record.vpc_id
  subnet_id           = module.vpc.record.private_subnet_ids[0]
  tailnet_auth_key    = var.tailnet_auth_key
  advertised_routes   = [var.vpc_cidr]
  enable_k8s_operator = var.enable_tailscale_operator
}

module "argo_bootstrap" {
  source = "../../../components/argo_bootstrap"

  depends_on = [
    # keep-sorted start
    module.dns,
    module.identity,
    module.network_mesh,
    # keep-sorted end
  ]

  cluster_name                  = module.cluster.record.cluster_name
  cluster_endpoint              = module.cluster.record.endpoint
  cluster_ca_certificate        = module.cluster.record.ca_certificate
  cluster_provider              = var.cluster_provider
  cluster_environment           = var.cluster_environment
  registered_cells              = var.registered_cells
  git_repo_url                  = var.git_repo_url
  git_ssh_private_key           = var.git_ssh_private_key
  git_repo_creds_url            = var.git_repo_creds_url
  enable_git_repo_creds         = var.enable_git_repo_creds
  target_revision               = var.target_revision
  fleet_availability            = var.fleet_availability
  domain_name                   = var.domain_name
  intranet_domain_name          = var.intranet_domain_name
  public_domain_name            = var.public_domain_name
  cluster_domain_name           = var.cluster_domain_name
  access_domain_name            = var.access_domain_name
  oidc_tls_insecure_skip_verify = var.oidc_tls_insecure_skip_verify
  cluster_labels                = var.cluster_labels
  annotations = merge(var.annotations, {
    "installation"     = try(var.annotations["installation"], var.resource_prefix != null && var.resource_prefix != "" ? var.resource_prefix : "corp")
    "aws-account-id"   = data.aws_caller_identity.current.account_id
    "backups-bucket"   = module.storage.record.buckets.backups.name
    "ecr-registry"     = module.registry.record.registry_url
    "profiles-bucket"  = module.storage.record.buckets.profiles.name
    "storage-endpoint" = "s3.${data.aws_region.current.region}.amazonaws.com"
    "athena-bucket"    = try(module.cloud_cost[0].record.bucket_name, "")
    "athena-database"  = try(module.cloud_cost[0].record.athena_database, "")
    "athena-workgroup" = try(module.cloud_cost[0].record.athena_workgroup, "")
  })
}

resource "tailscale_acl" "this" {
  count = var.enable_network_mesh && var.manage_tailscale_acl ? 1 : 0

  acl = var.enable_network_mesh && var.manage_tailscale_acl ? templatefile("${path.module}/../../../../argocd/components/tailscale_access/cloud-policy.hujson", {
    kubernetes_api_router_tags = {}
    resolver_route_cidrs       = []
    private_gateway_ipv4s      = []
  }) : "{}"
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c5842a6832f75f5727525e116e6494f615f7b42",
  ]
}

resource "aws_iam_role" "workspace_publisher" {
  name = "${var.cluster_name}-workspace-publisher"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = "repo:${coalesce(var.publisher_oidc_repository, local.github_repo_slug)}:ref:refs/heads/${local.publisher_branch}"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "workspace_publisher" {
  name = "${var.cluster_name}-workspace-publisher-policy"
  role = aws_iam_role.workspace_publisher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          # keep-sorted start
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:CreateRepository",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:TagResource",
          "ecr:UploadLayerPart",
          # keep-sorted end
        ]
        Resource = [
          "arn:aws:ecr:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:repository/src/*",
        ]
      }
    ]
  })
}

resource "aws_iam_user" "renovate_ecr_read" {
  name = "${var.cluster_name}-renovate-ecr-read"
}

resource "aws_iam_user_policy" "renovate_ecr_read" {
  name = "${var.cluster_name}-renovate-ecr-read-policy"
  user = aws_iam_user.renovate_ecr_read.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "ECRRead"
        Effect = "Allow"
        Action = [
          # keep-sorted start
          "ecr:BatchGetImage",
          "ecr:ListImages",
          # keep-sorted end
        ]
        Resource = [
          "arn:aws:ecr:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:repository/src/*",
        ]
      }
    ]
  })
}


