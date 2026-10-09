# Composes AWS cell infrastructure assembling VPC, EKS cluster, S3 storage, and IAM identity components.

data "aws_caller_identity" "current" {}

locals {
  create_vpc          = !contains(var.disabled_components, "vpc")
  create_cluster      = !contains(var.disabled_components, "cluster")
  enable_storage      = !contains(var.disabled_components, "storage")
  enable_identity     = var.enable_identity && !contains(var.disabled_components, "identity")
  enable_dns          = var.enable_dns && !contains(var.disabled_components, "dns")
  enable_secret_mgr   = !contains(var.disabled_components, "secret_manager")
  enable_network_mesh = var.enable_network_mesh && !contains(var.disabled_components, "network_mesh")
  enable_karpenter    = !contains(var.disabled_components, "karpenter")

  cluster_name           = var.cluster_name
  cluster_endpoint       = local.create_cluster ? module.cluster[0].record.endpoint : data.aws_eks_cluster.adopted[0].endpoint
  cluster_ca_certificate = local.create_cluster ? module.cluster[0].record.ca_certificate : data.aws_eks_cluster.adopted[0].certificate_authority[0].data
  oidc_issuer_url        = local.create_cluster ? module.cluster[0].record.oidc_issuer_url : data.aws_eks_cluster.adopted[0].identity[0].oidc[0].issuer
  oidc_provider_arn      = local.create_cluster ? module.cluster[0].record.oidc_provider_arn : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${replace(data.aws_eks_cluster.adopted[0].identity[0].oidc[0].issuer, "https://", "")}"

  vpc_id             = local.create_vpc ? module.vpc[0].record.vpc_id : try(data.aws_eks_cluster.adopted[0].vpc_config[0].vpc_id, "")
  private_subnet_ids = local.create_vpc ? module.vpc[0].record.private_subnet_ids : try(tolist(data.aws_eks_cluster.adopted[0].vpc_config[0].subnet_ids), [])
  public_subnet_ids  = local.create_vpc ? module.vpc[0].record.public_subnet_ids : []

  team_definition_files = fileset("${path.module}/../../../../definitions/teams", "*.yaml")
  teams = toset([
    for f in local.team_definition_files :
    yamldecode(file("${path.module}/../../../../definitions/teams/${f}")).slug
  ])

  system_callers = [
    "legacy-research-data",
    "org-workspace-backups",
    "storage-stats",
  ]

  # Deterministic 20-character uppercase access key ID: OPEN + 16 uppercase hex chars from sha1(caller)
  caller_access_key_ids = merge(
    {
      for caller in local.system_callers :
      caller => format("OPEN%s", upper(substr(sha1(caller), 0, 16)))
    },
    {
      for team in local.teams :
      team => format("OPEN%s", upper(substr(sha1(team), 0, 16)))
    },
    {
      for team in local.teams :
      "${team}-reader" => format("OPEN%s", upper(substr(sha1("${team}-reader"), 0, 16)))
    }
  )

  # Secret access keys generated per caller via random_password (length 40, alphanumeric)
  caller_secret_access_keys = {
    for caller, secret in random_password.caller_secret_access_key :
    caller => secret.result
  }

  # Gateway config secret values containing all callers (<caller>-access-key-id and <caller>-secret-access-key)
  s3_gateway_config_values = merge(
    {
      for caller in local.system_callers :
      "${caller}-access-key-id" => local.caller_access_key_ids[caller]
    },
    {
      for caller in local.system_callers :
      "${caller}-secret-access-key" => local.caller_secret_access_keys[caller]
    },
    {
      for team in local.teams :
      "${team}-access-key-id" => local.caller_access_key_ids[team]
    },
    {
      for team in local.teams :
      "${team}-secret-access-key" => local.caller_secret_access_keys[team]
    },
    {
      for team in local.teams :
      "${team}-reader-access-key-id" => local.caller_access_key_ids["${team}-reader"]
    },
    {
      for team in local.teams :
      "${team}-reader-secret-access-key" => local.caller_secret_access_keys["${team}-reader"]
    },
    var.global_storage != null ? {
      for team, creds in var.global_storage.teams :
      "${team}-global-access-key-id" => creds.access_key_id
    } : {},
    var.global_storage != null ? {
      for team, creds in var.global_storage.teams :
      "${team}-global-secret-access-key" => creds.secret_access_key
    } : {},
    var.global_storage != null ? {
      for team, creds in var.global_storage.readers :
      "${team}-reader-global-access-key-id" => creds.access_key_id
    } : {},
    var.global_storage != null ? {
      for team, creds in var.global_storage.readers :
      "${team}-reader-global-secret-access-key" => creds.secret_access_key
    } : {}
  )
}

data "aws_eks_cluster" "adopted" {
  count = local.create_cluster ? 0 : 1
  name  = var.cluster_name
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
  domain_name                   = var.domain_name
  parent_zone_id                = var.parent_zone_id
  tailnet_auth_key              = var.tailnet_auth_key
  fleet_availability            = var.fleet_availability
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_flow_logs              = var.enable_flow_logs
  disabled_components           = var.disabled_components
  iam_name_prefix               = var.iam_name_prefix
  kms_alias_prefix              = var.kms_alias_prefix
  iam_permissions_boundary      = var.iam_permissions_boundary
  tags                          = var.tags
  atlantis_plan_role_arn        = var.atlantis_plan_role_arn
  atlantis_apply_role_arn       = var.atlantis_apply_role_arn

  realized = {
    backups_bucket                  = local.enable_storage ? module.storage[0].record.buckets.backups.name : ""
    bucket_names                    = local.enable_storage ? { for k, b in module.storage[0].record.buckets : k => b.name } : {}
    cluster_name                    = local.cluster_name
    cluster_endpoint                = local.cluster_endpoint
    cluster_ca_certificate          = local.cluster_ca_certificate
    instance_profile_name           = "${var.iam_name_prefix}${var.cluster_name}-karpenter-node"
    karpenter_instance_profile_name = "${var.iam_name_prefix}${var.cluster_name}-karpenter-node"
    vpc_id                          = local.vpc_id
    private_subnet_ids              = local.private_subnet_ids
    public_subnet_ids               = local.public_subnet_ids
    zone_id                         = try(module.dns[0].record.zone_id, null)
    oidc_issuer_url                 = local.oidc_issuer_url
  }
}

module "vpc" {
  count  = local.create_vpc ? 1 : 0
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
  count  = local.create_cluster ? 1 : 0
  source = "../../../components/cluster/aws"

  cluster_name                  = var.cluster_name
  vpc_id                        = local.vpc_id
  subnet_ids                    = local.private_subnet_ids
  pod_subnet_ids                = local.create_vpc ? module.vpc[0].record.pod_subnet_ids : []
  kubernetes_version            = var.kubernetes_version
  service_ipv4_cidr             = var.service_ipv4_cidr
  public_access_cidrs           = var.public_access_cidrs
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_control_plane_logging  = var.enable_control_plane_logging
  enable_access_config          = var.enable_access_config
  enable_addons                 = var.enable_addons
  enable_ebs_csi                = var.enable_ebs_csi
  enable_karpenter_interruption = local.enable_karpenter && local.enable_identity
  system_instance_types         = var.system_instance_types
  node_repair_enabled           = var.node_repair_enabled
  iam_name_prefix               = var.iam_name_prefix
  kms_alias_prefix              = var.kms_alias_prefix
  iam_permissions_boundary      = var.iam_permissions_boundary
  tags                          = var.tags
  atlantis_plan_role_arn        = var.atlantis_plan_role_arn
  atlantis_apply_role_arn       = var.atlantis_apply_role_arn
}

module "storage" {
  count  = local.enable_storage ? 1 : 0
  source = "../../../components/storage/aws"

  account_id       = data.aws_caller_identity.current.account_id
  cluster_name     = var.cluster_name
  kms_alias_prefix = var.kms_alias_prefix
}

module "identity" {
  count  = local.enable_identity ? 1 : 0
  source = "../../../components/identity/aws"

  cluster_name             = var.cluster_name
  cluster_oidc_issuer_url  = local.oidc_issuer_url
  cluster_oidc_arn         = local.oidc_provider_arn
  iam_name_prefix          = var.iam_name_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
  shared_secret_names      = var.shared_secret_names
  storage_kms_key_arn      = local.enable_storage ? module.storage[0].kms_key_arn : ""
  storage_meta_bucket_arn  = local.enable_storage ? module.storage[0].record.buckets.meta.arn : ""

  roles = merge(
    {
      # keep-sorted start block=yes
      cert-manager = {
        namespace       = "cert-manager-system"
        service_account = "cert-manager"
      }
      db-backups = {
        namespace       = "database"
        service_account = "barman"
      }
      external-dns = {
        namespace       = "external-dns-system"
        service_account = "external-dns"
      }
      external-secrets = {
        namespace       = "external-secrets-system"
        service_account = "external-secrets"
      }
      kopia = {
        namespace       = "coder"
        service_account = "kopia"
      }
      legacy-research-data = {
        namespace       = "s3-system"
        service_account = "s3-gateway-legacy-research-data"
      }
      load-balancer = {
        namespace       = "kube-system"
        service_account = "aws-load-balancer-controller"
      }
      opencost = {
        namespace       = "opencost"
        service_account = "opencost"
      }
      prowler = {
        namespace       = "prowler"
        service_account = "prowler"
      }
      storage-stats = {
        namespace       = "s3-system"
        service_account = "s3-gateway-storage-stats"
      }
      trivy = {
        namespace       = "trivy-system"
        service_account = "trivy-operator"
      }
      velero = {
        namespace       = "velero-system"
        service_account = "velero-server"
      }
      workspace-backups = {
        namespace       = "s3-system"
        service_account = "s3-gateway-org-workspace-backups"
      }
      workspace-ecr = {
        namespace       = "workspaces"
        service_account = "coder-workspace"
      }
      # keep-sorted end
    },
    {
      for team in local.teams : "s3-gateway-${team}" => {
        namespace       = "s3-system"
        service_account = "s3-gateway-${team}"
      }
    },
    {
      for team in local.teams : "s3-gateway-${team}-reader" => {
        namespace       = "s3-system"
        service_account = "s3-gateway-${team}-reader"
      }
    },
    local.enable_karpenter ? {
      karpenter = {
        namespace       = "karpenter-system"
        service_account = "karpenter"
      }
    } : {}
  )
}

module "dns" {
  count  = local.enable_dns ? 1 : 0
  source = "../../../components/dns/aws"

  domain_name    = var.domain_name
  is_cell        = true
  parent_zone_id = var.parent_zone_id
}

resource "random_password" "caller_secret_access_key" {
  for_each = setunion(
    toset(local.system_callers),
    local.teams,
    toset([for team in local.teams : "${team}-reader"])
  )

  length  = 40
  special = false
}

module "s3_gateway_config_secret" {
  count  = local.enable_secret_mgr ? 1 : 0
  source = "../../../components/secret_manager/aws"

  kms_alias_prefix = var.kms_alias_prefix
  secret_name      = "${var.cluster_name}-s3-gateway-config"
  secret_values    = local.s3_gateway_config_values
}

module "s3_team_secrets" {
  for_each = local.enable_secret_mgr ? local.teams : toset([])
  source   = "../../../components/secret_manager/aws"

  kms_alias_prefix = var.kms_alias_prefix
  secret_name      = "${var.cluster_name}-s3-team-${each.key}"
  secret_values = merge(
    {
      access_key_id     = local.caller_access_key_ids[each.key]
      secret_access_key = local.caller_secret_access_keys[each.key]
    },
    var.global_storage != null ? {
      global_access_key_id     = var.global_storage.teams[each.key].access_key_id
      global_secret_access_key = var.global_storage.teams[each.key].secret_access_key
      global_endpoint          = var.global_storage.endpoint
      global_bucket            = var.global_storage.teams[each.key].bucket
    } : {}
  )
}

module "s3_team_reader_secrets" {
  for_each = local.enable_secret_mgr ? local.teams : toset([])
  source   = "../../../components/secret_manager/aws"

  kms_alias_prefix = var.kms_alias_prefix
  secret_name      = "${var.cluster_name}-s3-team-${each.key}-reader"
  secret_values = merge(
    {
      access_key_id     = local.caller_access_key_ids["${each.key}-reader"]
      secret_access_key = local.caller_secret_access_keys["${each.key}-reader"]
    },
    var.global_storage != null ? {
      global_access_key_id     = var.global_storage.readers[each.key].access_key_id
      global_secret_access_key = var.global_storage.readers[each.key].secret_access_key
      global_endpoint          = var.global_storage.endpoint
      global_bucket            = var.global_storage.readers[each.key].bucket
    } : {}
  )
}

module "s3_workspace_backups_secret" {
  count  = local.enable_secret_mgr ? 1 : 0
  source = "../../../components/secret_manager/aws"

  kms_alias_prefix = var.kms_alias_prefix
  secret_name      = "${var.cluster_name}-s3-org-workspace-backups"
  secret_values = {
    access_key_id     = local.caller_access_key_ids["org-workspace-backups"]
    secret_access_key = local.caller_secret_access_keys["org-workspace-backups"]
  }
}

module "s3_legacy_research_data_secret" {
  count  = local.enable_secret_mgr ? 1 : 0
  source = "../../../components/secret_manager/aws"

  kms_alias_prefix = var.kms_alias_prefix
  secret_name      = "${var.cluster_name}-s3-legacy-research-data"
  secret_values = {
    access_key_id     = local.caller_access_key_ids["legacy-research-data"]
    secret_access_key = local.caller_secret_access_keys["legacy-research-data"]
  }
}

module "network_mesh" {
  count  = local.enable_network_mesh ? 1 : 0
  source = "../../../components/network_mesh/aws"

  name                     = var.cluster_name
  cluster_name             = var.cluster_name
  vpc_id                   = local.vpc_id
  subnet_id                = local.private_subnet_ids[0]
  tailnet_auth_key         = var.tailnet_auth_key
  advertised_routes        = [var.vpc_cidr]
  clamp_tunnel_mss         = true
  masquerade_tunnel_egress = true
  iam_name_prefix          = var.iam_name_prefix
  kms_alias_prefix         = var.kms_alias_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
}

locals {
  mesh_peer_routes = merge(
    {
      for pair in setproduct(var.mesh_peer_cidrs, range(length(try(module.vpc[0].record.private_route_table_ids, [])))) :
      "${pair[0]}-private-${pair[1]}" => {
        cidr           = pair[0]
        route_table_id = module.vpc[0].record.private_route_table_ids[pair[1]]
      } if local.enable_network_mesh
    },
    {
      for pair in setproduct(var.mesh_peer_cidrs, range(length(try(module.vpc[0].record.pod_route_table_ids, [])))) :
      "${pair[0]}-pod-${pair[1]}" => {
        cidr           = pair[0]
        route_table_id = module.vpc[0].record.pod_route_table_ids[pair[1]]
      } if local.enable_network_mesh
    },
  )
}

resource "aws_route" "mesh_peer" {
  for_each = local.mesh_peer_routes

  route_table_id         = each.value.route_table_id
  destination_cidr_block = each.value.cidr
  network_interface_id   = module.network_mesh[0].record.primary_network_interface_id
}

moved {
  from = aws_sqs_queue.karpenter_interruption
  to   = module.cluster[0].aws_sqs_queue.karpenter_interruption
}

moved {
  from = aws_sqs_queue_policy.karpenter_interruption
  to   = module.cluster[0].aws_sqs_queue_policy.karpenter_interruption
}

moved {
  from = aws_cloudwatch_event_rule.karpenter_spot_interruption
  to   = module.cluster[0].aws_cloudwatch_event_rule.karpenter_spot_interruption
}

moved {
  from = aws_cloudwatch_event_target.karpenter_spot_interruption
  to   = module.cluster[0].aws_cloudwatch_event_target.karpenter_spot_interruption
}

moved {
  from = aws_cloudwatch_event_rule.karpenter_rebalance
  to   = module.cluster[0].aws_cloudwatch_event_rule.karpenter_rebalance
}

moved {
  from = aws_cloudwatch_event_target.karpenter_rebalance
  to   = module.cluster[0].aws_cloudwatch_event_target.karpenter_rebalance
}

moved {
  from = aws_cloudwatch_event_rule.karpenter_instance_state_change
  to   = module.cluster[0].aws_cloudwatch_event_rule.karpenter_instance_state_change
}

moved {
  from = aws_cloudwatch_event_target.karpenter_instance_state_change
  to   = module.cluster[0].aws_cloudwatch_event_target.karpenter_instance_state_change
}

moved {
  from = aws_cloudwatch_event_rule.karpenter_health_event
  to   = module.cluster[0].aws_cloudwatch_event_rule.karpenter_health_event
}

moved {
  from = aws_cloudwatch_event_target.karpenter_health_event
  to   = module.cluster[0].aws_cloudwatch_event_target.karpenter_health_event
}

