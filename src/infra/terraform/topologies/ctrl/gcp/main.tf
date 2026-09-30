# Composes GCP control plane infrastructure assembling VPC, GKE, Artifact Registry, and Cloud DNS.

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
  source = "../../../components/vpc/gcp"

  name               = "${var.cluster_name}-vpc"
  cidr_block         = var.vpc_cidr
  availability_zones = var.availability_zones
  tier_subnets       = var.tier_subnets
  enable_flow_logs   = var.enable_flow_logs
}

module "cluster" {
  source = "../../../components/cluster/gcp"

  cluster_name                  = var.cluster_name
  vpc_id                        = module.vpc.record.vpc_id
  subnet_ids                    = module.vpc.record.private_subnet_ids
  kubernetes_version            = var.kubernetes_version
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_control_plane_logging  = var.enable_control_plane_logging
}

module "storage" {
  source = "../../../components/storage/gcp"

  installation_name = var.resource_prefix != null && var.resource_prefix != "" ? var.resource_prefix : "cloud"
  cell_name         = var.cluster_name
  storage_tiers     = ["home", "scratch", "archive", "backups", "meta", "logs", "profiles"]
}

module "registry" {
  source = "../../../components/registry/gcp"

  installation_name = var.cluster_name
}

module "workload_registry" {
  count  = 0
  source = "../../../components/registry/gcp"

  installation_name = var.cluster_name
}

module "identity" {
  count  = var.enable_identity ? 1 : 0
  source = "../../../components/identity/gcp"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = module.cluster.record.oidc_issuer_url
  project_id              = local.project

  roles = {
    # keep-sorted start block=yes
    atlantis = {
      namespace       = "atlantis"
      service_account = "atlantis"
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
  source = "../../../components/cloud_cost/gcp"

  cluster_name = var.cluster_name
  project_id   = local.project
}


module "dns" {
  count  = var.enable_dns ? 1 : 0
  source = "../../../components/dns/gcp"

  domain_name = var.domain_name
  is_cell     = false
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
  cluster_provider              = "gcp"
  cluster_environment           = "production"
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
  cluster_labels                = var.cluster_labels
  annotations = merge(var.annotations, {
    "artifact-registry"   = try(module.workload_registry[0].record.registry_host, "")
    "backups-bucket"      = module.storage.record.buckets.backups.name
    "gcp-billing-dataset" = try(module.cloud_cost[0].record.billing_dataset, "")
    "gcp-location"        = local.location
    "gcp-project-id"      = local.project
    "gcp-project-number"  = tostring(data.google_project.current.number)
    "profiles-bucket"     = module.storage.record.buckets.profiles.name
    "storage-endpoint"    = "storage.googleapis.com"
  })
}

resource "tailscale_acl" "this" {
  count = var.enable_network_mesh ? 1 : 0

  acl = var.enable_network_mesh ? templatefile("${path.module}/../../../../argocd/components/tailscale_access/cloud-policy.hujson", {
    kubernetes_api_router_tags = {}
    resolver_route_cidrs       = []
    private_gateway_ipv4s      = []
  }) : "{}"
}
