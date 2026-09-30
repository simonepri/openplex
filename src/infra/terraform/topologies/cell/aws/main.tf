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

  realized = {
    backups_bucket         = local.enable_storage ? module.storage[0].record.buckets.backups.name : ""
    cluster_name           = local.cluster_name
    cluster_endpoint       = local.cluster_endpoint
    cluster_ca_certificate = local.cluster_ca_certificate
    vpc_id                 = local.vpc_id
    private_subnet_ids     = local.private_subnet_ids
    public_subnet_ids      = local.public_subnet_ids
    zone_id                = try(module.dns[0].record.zone_id, null)
    oidc_issuer_url        = local.oidc_issuer_url
  }
}

module "vpc" {
  count  = local.create_vpc ? 1 : 0
  source = "../../../components/vpc/aws"

  name               = "${var.cluster_name}-vpc"
  cidr_block         = var.vpc_cidr
  availability_zones = var.availability_zones
  tier_subnets       = var.tier_subnets
  enable_flow_logs   = var.enable_flow_logs
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
}

module "storage" {
  count  = local.enable_storage ? 1 : 0
  source = "../../../components/storage/aws"

  installation_name = var.resource_prefix != null && var.resource_prefix != "" ? var.resource_prefix : "cloud"
  cell_name         = var.cluster_name
}

module "identity" {
  count  = local.enable_identity ? 1 : 0
  source = "../../../components/identity/aws"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = local.oidc_issuer_url
  cluster_oidc_arn        = local.oidc_provider_arn
  shared_secret_names     = var.shared_secret_names
  storage_kms_key_arn     = local.enable_storage ? module.storage[0].kms_key_arn : ""

  roles = merge(
    {
      # keep-sorted start block=yes
      "examples-workspace-ecr" = {
        namespace       = "coder"
        service_account = "workspace-ecr"
      }
      aws_load_balancer_controller = {
        namespace       = "kube-system"
        service_account = "aws-load-balancer-controller"
      }
      barman = {
        namespace       = "database"
        service_account = "barman"
      }
      cloud_telemetry = {
        namespace       = "otel-system"
        service_account = "otel-collector"
      }
      external_dns = {
        namespace       = "external-dns-system"
        service_account = "external-dns"
      }
      external_secrets = {
        namespace       = "external-secrets-system"
        service_account = "external-secrets"
      }
      kopia = {
        namespace       = "coder"
        service_account = "kopia"
      }
      prowler = {
        namespace       = "prowler"
        service_account = "prowler"
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

module "dns" {
  count  = local.enable_dns ? 1 : 0
  source = "../../../components/dns/aws"

  domain_name    = var.domain_name
  is_cell        = true
  parent_zone_id = var.parent_zone_id
}

module "secret_manager" {
  count  = local.enable_secret_mgr ? 1 : 0
  source = "../../../components/secret_manager/aws"

  secret_name = "${var.cluster_name}-platform-secrets"
  secret_values = {
    placeholder = "initialized"
  }
}

module "network_mesh" {
  count  = local.enable_network_mesh ? 1 : 0
  source = "../../../components/network_mesh/aws"

  name              = var.cluster_name
  vpc_id            = local.vpc_id
  subnet_id         = local.private_subnet_ids[0]
  tailnet_auth_key  = var.tailnet_auth_key
  advertised_routes = [var.vpc_cidr]
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

