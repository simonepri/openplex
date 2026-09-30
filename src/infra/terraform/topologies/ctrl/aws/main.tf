# Composes AWS control plane infrastructure assembling VPC, EKS, ECR registry, and Route 53 DNS.

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
  }
}

module "vpc" {
  source = "../../../components/vpc/aws"

  name               = "${var.cluster_name}-vpc"
  cidr_block         = var.vpc_cidr
  availability_zones = var.availability_zones
  tier_subnets       = var.tier_subnets
  enable_flow_logs   = var.enable_flow_logs
}

module "cluster" {
  source = "../../../components/cluster/aws"

  cluster_name                  = var.cluster_name
  vpc_id                        = module.vpc.record.vpc_id
  subnet_ids                    = module.vpc.record.private_subnet_ids
  kubernetes_version            = var.kubernetes_version
  service_ipv4_cidr             = var.service_ipv4_cidr
  public_access_cidrs           = var.public_access_cidrs
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_control_plane_logging  = var.enable_control_plane_logging
  enable_access_config          = var.enable_access_config
  enable_addons                 = var.enable_addons
  enable_ebs_csi                = var.enable_ebs_csi
  system_instance_types         = var.system_instance_types
}

module "storage" {
  source = "../../../components/storage/aws"

  installation_name = var.resource_prefix != null && var.resource_prefix != "" ? var.resource_prefix : "cloud"
  cell_name         = var.cluster_name
}

data "aws_caller_identity" "current" {}

module "registry" {
  source = "../../../components/registry/aws"

  installation_name = var.cluster_name
}

module "identity" {
  count  = var.enable_identity ? 1 : 0
  source = "../../../components/identity/aws"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = module.cluster.record.oidc_issuer_url
  cluster_oidc_arn        = module.cluster.record.oidc_provider_arn

  roles = {
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
    cert_manager = {
      namespace       = "cert-manager-system"
      service_account = "cert-manager"
    }
    cloud_telemetry = {
      namespace       = "otel-system"
      service_account = "otel-collector"
    }
    external_dns = {
      namespace       = "external-dns-system"
      service_account = "external-dns"
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
  }
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

module "secret_manager" {
  source = "../../../components/secret_manager/aws"

  secret_name = "${var.cluster_name}-platform-secrets"
  secret_values = {
    placeholder = "initialized"
  }
}

module "network_mesh" {
  count  = var.enable_network_mesh ? 1 : 0
  source = "../../../components/network_mesh/aws"

  name              = var.cluster_name
  vpc_id            = module.vpc.record.vpc_id
  subnet_id         = module.vpc.record.private_subnet_ids[0]
  tailnet_auth_key  = var.tailnet_auth_key
  advertised_routes = [var.vpc_cidr]
}

module "argo_bootstrap" {
  source = "../../../components/argo_bootstrap"

  depends_on = [module.network_mesh]

  cluster_name                  = module.cluster.record.cluster_name
  cluster_endpoint              = module.cluster.record.endpoint
  cluster_ca_certificate        = module.cluster.record.ca_certificate
  cluster_provider              = var.cluster_provider
  cluster_environment           = var.cluster_environment
  registered_cells              = var.registered_cells
  git_repo_url                  = var.git_repo_url
  target_revision               = var.target_revision
  fleet_availability            = var.fleet_availability
  domain_name                   = var.domain_name
  intranet_domain_name          = var.intranet_domain_name
  public_domain_name            = var.public_domain_name
  cluster_domain_name           = var.cluster_domain_name
  access_domain_name            = var.access_domain_name
  oidc_tls_insecure_skip_verify = var.oidc_tls_insecure_skip_verify
  annotations = merge(var.annotations, {
    "aws-account-id"   = data.aws_caller_identity.current.account_id
    "backups-bucket"   = module.storage.record.buckets.backups.name
    "ecr-registry"     = module.registry.record.registry_url
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
