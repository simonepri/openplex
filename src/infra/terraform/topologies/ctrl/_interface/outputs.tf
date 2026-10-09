# Exports provider-neutral output records for control plane cluster, VPC, registry, and DNS components.

output "record" {
  description = "Standardized record of the control plane cluster."
  value       = var.realized
}

output "config" {
  description = "Standardized input configuration contract for the control plane cluster."
  sensitive   = true
  value = {
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
    enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
    enable_flow_logs              = var.enable_flow_logs
    access_domain_name            = var.access_domain_name
    cluster_domain_name           = var.cluster_domain_name
    intranet_domain_name          = var.intranet_domain_name
    public_domain_name            = var.public_domain_name
    oidc_tls_insecure_skip_verify = var.oidc_tls_insecure_skip_verify
    domain_name                   = var.domain_name
    tailnet_auth_key_configured   = length(var.tailnet_auth_key) > 0
    git_repo_url                  = var.git_repo_url
    target_revision               = var.target_revision
    registered_cells              = var.registered_cells
    fleet_availability            = var.fleet_availability
    annotations                   = var.annotations
    disabled_components           = var.disabled_components
    profiles_retention_days       = var.profiles_retention_days
    iam_name_prefix               = var.iam_name_prefix
    kms_alias_prefix              = var.kms_alias_prefix
    iam_permissions_boundary      = var.iam_permissions_boundary
    tags                          = var.tags
    account_id                    = var.account_id
    atlantis_plan_role_arn        = var.atlantis_plan_role_arn
    atlantis_apply_role_arn       = var.atlantis_apply_role_arn
  }
}
