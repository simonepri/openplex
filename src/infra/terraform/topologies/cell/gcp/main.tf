# Composes GCP cell infrastructure assembling VPC, GKE cluster, GCS storage, and IAM identity components.

data "google_client_config" "current" {}
data "google_project" "current" {}

locals {
  project  = coalesce(data.google_client_config.current.project, "default")
  location = coalesce(data.google_client_config.current.region, data.google_client_config.current.zone, "us-central1")
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
    backups_bucket         = module.storage.record.buckets.backups.name
    cluster_name           = module.cluster.record.cluster_name
    cluster_endpoint       = module.cluster.record.endpoint
    cluster_ca_certificate = module.cluster.record.ca_certificate
    vpc_id                 = module.vpc.record.vpc_id
    private_subnet_ids     = module.vpc.record.private_subnet_ids
    public_subnet_ids      = module.vpc.record.public_subnet_ids
    zone_id                = try(module.dns[0].record.zone_id, null)
    oidc_issuer_url        = module.cluster.record.oidc_issuer_url
    name_servers           = try(module.dns[0].record.name_servers, [])
  }
}

module "vpc" {
  source = "../../../components/vpc/gcp"

  name               = "${var.cluster_name}-vpc"
  cidr_block         = var.vpc_cidr
  availability_zones = var.availability_zones
  tier_subnets       = var.tier_subnets
  enable_flow_logs   = var.enable_flow_logs
  service_cidr       = var.service_ipv4_cidr != null ? var.service_ipv4_cidr : "10.96.0.0/16"
}

module "cluster" {
  source = "../../../components/cluster/gcp"

  cluster_name                  = var.cluster_name
  vpc_id                        = module.vpc.record.vpc_id
  subnet_ids                    = module.vpc.record.private_subnet_ids
  kubernetes_version            = var.kubernetes_version
  master_ipv4_cidr_block        = var.master_ipv4_cidr_block
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_control_plane_logging  = var.enable_control_plane_logging
}

module "storage" {
  source = "../../../components/storage/gcp"

  installation_name = var.resource_prefix != null && var.resource_prefix != "" ? var.resource_prefix : "cloud"
  cell_name         = var.cluster_name
  location          = local.location
}

module "identity" {
  count  = var.enable_identity ? 1 : 0
  source = "../../../components/identity/gcp"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = module.cluster.record.oidc_issuer_url
  project_id              = local.project

  roles = {
    # keep-sorted start block=yes
    barman = {
      namespace       = "database"
      service_account = "barman"
    }
    external_dns = {
      namespace       = "external-dns-system"
      service_account = "external-dns"
    }
    karpenter = {
      namespace       = "karpenter-system"
      service_account = "karpenter"
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
  }
}

module "dns" {
  count  = var.enable_dns ? 1 : 0
  source = "../../../components/dns/gcp"

  domain_name = var.domain_name
  is_cell     = true
}

module "secret_manager" {
  source = "../../../components/secret_manager/gcp"

  secret_name = "${var.cluster_name}-platform-secrets"
  secret_values = {
    placeholder = "initialized"
  }
}

module "network_mesh" {
  count  = var.enable_network_mesh ? 1 : 0
  source = "../../../components/network_mesh/gcp"

  name              = var.cluster_name
  vpc_id            = module.vpc.record.vpc_id
  subnet_id         = module.vpc.record.private_subnet_ids[0]
  tailnet_auth_key  = var.tailnet_auth_key
  advertised_routes = [var.vpc_cidr]
}
