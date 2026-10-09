# Composes AWS control plane infrastructure assembling VPC, EKS, ECR registry, and Route 53 DNS.

locals {
  cluster_name            = var.cluster_name
  tags                    = var.tags
  enable_karpenter        = !contains(var.disabled_components, "karpenter")
  enable_secret_mgr       = !contains(var.disabled_components, "secret_manager")
  github_repo_slug        = try(regex("(?:github\\.com[:/])([^/]+/[^/.]+?)(?:\\.git)?$", var.git_repo_url)[0], "openplex/openplex")
  publisher_branch        = var.target_revision == "HEAD" ? "main" : var.target_revision
  account_id              = coalesce(var.account_id, data.aws_caller_identity.current.account_id)
  atlantis_plan_role_arn  = length(var.atlantis_plan_role_arn) > 0 ? var.atlantis_plan_role_arn : (var.enable_identity && length(module.identity) > 0 ? module.identity[0].atlantis_plan_role_arn : "")
  atlantis_apply_role_arn = length(var.atlantis_apply_role_arn) > 0 ? var.atlantis_apply_role_arn : (var.enable_identity && length(module.identity) > 0 ? module.identity[0].atlantis_apply_role_arn : "")
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
  iam_name_prefix               = var.iam_name_prefix
  kms_alias_prefix              = var.kms_alias_prefix
  iam_permissions_boundary      = var.iam_permissions_boundary
  tags                          = var.tags
  account_id                    = local.account_id
  atlantis_plan_role_arn        = var.atlantis_plan_role_arn
  atlantis_apply_role_arn       = var.atlantis_apply_role_arn

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

  name                     = "${var.cluster_name}-vpc"
  cidr_block               = var.vpc_cidr
  availability_zones       = var.availability_zones
  tier_subnets             = var.tier_subnets
  enable_flow_logs         = var.enable_flow_logs
  cluster_name             = var.cluster_name
  iam_name_prefix          = var.iam_name_prefix
  kms_alias_prefix         = var.kms_alias_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
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
  iam_name_prefix               = var.iam_name_prefix
  kms_alias_prefix              = var.kms_alias_prefix
  iam_permissions_boundary      = var.iam_permissions_boundary
  tags                          = var.tags
  atlantis_plan_role_arn        = local.atlantis_plan_role_arn
  atlantis_apply_role_arn       = local.atlantis_apply_role_arn
}

module "storage" {
  source = "../../../components/storage/aws"

  cluster_name            = var.cluster_name
  account_id              = local.account_id
  kms_alias_prefix        = var.kms_alias_prefix
  storage_tiers           = ["home", "scratch", "archive", "backups", "meta", "logs", "profiles"]
  profiles_retention_days = var.profiles_retention_days
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

module "registry" {
  source = "../../../components/registry/aws"

  cluster_name     = var.cluster_name
  kms_alias_prefix = var.kms_alias_prefix
  repositories     = ["infrastructure"]
}

module "identity" {
  count  = var.enable_identity ? 1 : 0
  source = "../../../components/identity/aws"

  cluster_name                    = var.cluster_name
  cluster_oidc_issuer_url         = module.cluster.record.oidc_issuer_url
  cluster_oidc_arn                = module.cluster.record.oidc_provider_arn
  shared_secret_names             = var.shared_secret_names
  storage_kms_key_arn             = module.storage.kms_key_arn
  storage_meta_bucket_arn         = module.storage.record.buckets.meta.arn
  storage_stats_inventory_reports = var.storage_stats_inventory_reports
  iam_name_prefix                 = var.iam_name_prefix
  iam_permissions_boundary        = var.iam_permissions_boundary
  opentofu_state_bucket           = var.opentofu_state_bucket

  roles = merge(
    {
      # keep-sorted start block=yes
      "atlantis" = {
        namespace       = "atlantis"
        service_account = "atlantis-apply"
      }
      "buildbuddy-backups" = {
        namespace       = "buildbuddy"
        service_account = "buildbuddy-postgres"
      }
      "cert-manager" = {
        namespace       = "cert-manager-system"
        service_account = "cert-manager"
      }
      "clickhouse" = {
        namespace       = "signoz"
        service_account = "signoz-clickhouse"
      }
      "cloud-telemetry" = {
        namespace       = "otel-system"
        service_account = "cloud-telemetry"
      }
      "coder-backups" = {
        namespace       = "coder"
        service_account = "coder-postgres"
      }
      "dragonfly-backups" = {
        namespace       = "dragonfly-system"
        service_account = "dragonfly-postgres"
      }
      "external-dns" = {
        namespace       = "external-dns-system"
        service_account = "external-dns"
      }
      "external-secrets" = {
        namespace       = "external-secrets-system"
        service_account = "external-secrets"
      }
      "kargo" = {
        namespace       = "kargo"
        service_account = "kargo-controller"
      }
      "load-balancer" = {
        namespace       = "kube-system"
        service_account = "aws-load-balancer-controller"
      }
      "parca" = {
        namespace       = "parca"
        service_account = "parca"
      }
      "prowler" = {
        namespace       = "prowler"
        service_account = "prowler"
      }
      "signoz-backups" = {
        namespace       = "signoz"
        service_account = "signoz-postgres"
      }
      "snapshot-portal" = {
        namespace       = "coder-workspace-backup-system"
        service_account = "coder-snapshot-portal"
      }
      "trivy" = {
        namespace       = "trivy-system"
        service_account = "trivy-operator"
      }
      "velero" = {
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

  cluster_name             = var.cluster_name
  account_id               = local.account_id
  iam_name_prefix          = var.iam_name_prefix
  kms_alias_prefix         = var.kms_alias_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
  cluster_oidc_issuer_url  = module.cluster.record.oidc_issuer_url
  cluster_oidc_arn         = module.cluster.record.oidc_provider_arn
}

module "cloud_trail" {
  source = "../../../components/cloud_trail/aws"

  cluster_name              = local.cluster_name
  s3_data_event_bucket_arns = var.s3_data_event_bucket_arns
  tags                      = local.tags
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

  name                     = var.cluster_name
  cluster_name             = var.cluster_name
  vpc_id                   = module.vpc.record.vpc_id
  subnet_id                = module.vpc.record.private_subnet_ids[0]
  tailnet_auth_key         = var.tailnet_auth_key
  advertised_routes        = [var.vpc_cidr]
  clamp_tunnel_mss         = true
  masquerade_tunnel_egress = true
  enable_k8s_operator      = var.enable_tailscale_operator
  iam_name_prefix          = var.iam_name_prefix
  kms_alias_prefix         = var.kms_alias_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
}

resource "random_password" "workspace_snapshot_root_key" {
  length  = 64
  special = false
}

module "workspace_snapshot_root_secret" {
  count  = local.enable_secret_mgr ? 1 : 0
  source = "../../../components/secret_manager/aws"

  kms_alias_prefix = var.kms_alias_prefix
  secret_name      = "${var.cluster_name}-workspace-snapshot-root"
  secret_values = {
    root_key = random_password.workspace_snapshot_root_key.result
  }
}

locals {
  mesh_peer_routes = merge(
    {
      for pair in setproduct(var.mesh_peer_cidrs, range(length(try(module.vpc.record.private_route_table_ids, [])))) :
      "${pair[0]}-private-${pair[1]}" => {
        cidr           = pair[0]
        route_table_id = module.vpc.record.private_route_table_ids[pair[1]]
      } if var.enable_network_mesh && length(module.network_mesh) > 0
    },
    {
      for pair in setproduct(var.mesh_peer_cidrs, range(length(try(module.vpc.record.pod_route_table_ids, [])))) :
      "${pair[0]}-pod-${pair[1]}" => {
        cidr           = pair[0]
        route_table_id = module.vpc.record.pod_route_table_ids[pair[1]]
      } if var.enable_network_mesh && length(module.network_mesh) > 0
    },
  )
}

resource "aws_route" "mesh_peer" {
  for_each = local.mesh_peer_routes

  route_table_id         = each.value.route_table_id
  destination_cidr_block = each.value.cidr
  network_interface_id   = module.network_mesh[0].record.primary_network_interface_id
}

module "argo_bootstrap" {
  source = "../../../components/argo_bootstrap"

  depends_on = [
    # keep-sorted start
    aws_route.mesh_peer,
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
  annotations                   = local.ctrl_cluster_annotations
  atlantis_plan_role_arn        = local.atlantis_plan_role_arn
  atlantis_apply_role_arn       = local.atlantis_apply_role_arn
}

locals {
  ctrl_cluster_annotations = merge(
    var.annotations,
    {
      "aws-account-id"   = local.account_id
      "resource-tags"    = jsonencode(var.tags)
      "backups-bucket"   = module.storage.record.buckets.backups.name
      "ecr-registry"     = module.registry.record.registry_url
      "profiles-bucket"  = module.storage.record.buckets.profiles.name
      "storage-endpoint" = "s3.${data.aws_region.current.region}.amazonaws.com"
      "athena-bucket"    = try(module.cloud_cost[0].record.bucket_name, "")
      "athena-database"  = try(module.cloud_cost[0].record.athena_database, "")
      "athena-table"     = try(module.cloud_cost[0].table_name, "")
      "athena-workgroup" = try(module.cloud_cost[0].record.athena_workgroup, "")

      "opencost-cloud-cost-reader" = var.enable_cloud_cost ? "true" : "false"
    },
    length(local.atlantis_plan_role_arn) > 0 ? {
      "atlantis-plan-role-arn" = local.atlantis_plan_role_arn
    } : {},
    length(local.atlantis_apply_role_arn) > 0 ? {
      "atlantis-apply-role-arn" = local.atlantis_apply_role_arn
    } : {},
  )
}

resource "tailscale_acl" "this" {
  count = var.enable_network_mesh && var.manage_tailscale_acl ? 1 : 0

  acl = var.enable_network_mesh && var.manage_tailscale_acl ? templatefile("${path.module}/../../../../argocd/components/tailscale_access/cloud-policy.hujson", {
    kubernetes_api_router_tags = {}
    resolver_route_cidrs       = []
    private_gateway_ipv4s      = []
    cell_service_cidrs         = var.cell_service_cidrs
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
  name                 = "${var.iam_name_prefix}${var.cluster_name}-workspace-publisher"
  permissions_boundary = var.iam_permissions_boundary

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
  name = "${var.iam_name_prefix}${var.cluster_name}-workspace-publisher-policy"
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
          "ecr:PutImageTagMutability",
          "ecr:PutLifecyclePolicy",
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
  name = "${var.iam_name_prefix}${var.cluster_name}-renovate-ecr-read"
}

resource "aws_iam_user_policy" "renovate_ecr_read" {
  name = "${var.iam_name_prefix}${var.cluster_name}-renovate-ecr-read-policy"
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

resource "aws_s3_account_public_access_block" "account" {
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_ebs_encryption_by_default" "this" {
  enabled = true
}

resource "aws_iam_account_password_policy" "strict" {
  minimum_password_length        = 14
  require_lowercase_characters   = true
  require_numbers                = true
  require_uppercase_characters   = true
  require_symbols                = true
  allow_users_to_change_password = true
  password_reuse_prevention      = 24
  max_password_age               = 90
}

resource "aws_iam_role" "support" {
  name = "${var.iam_name_prefix}${var.cluster_name}-incident-support-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AssumeRoleForSupport"
        Effect = "Allow"
        Action = "sts:AssumeRole"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
      }
    ]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "support" {
  role       = aws_iam_role.support.name
  policy_arn = "arn:aws:iam::aws:policy/AWSSupportAccess"
}


