# Exports provider-neutral output records for cell cluster, VPC, and storage components.

output "record" {
  description = "Standardized record of the workload cell cluster."
  value       = var.realized
}

output "config" {
  description = "Standardized input configuration contract for the workload cell cluster."
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
    enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
    enable_flow_logs              = var.enable_flow_logs
    domain_name                   = var.domain_name
    parent_zone_id                = var.parent_zone_id
    tailnet_auth_key_configured   = length(var.tailnet_auth_key) > 0
    fleet_availability            = var.fleet_availability
    disabled_components           = var.disabled_components
    iam_name_prefix               = var.iam_name_prefix
    kms_alias_prefix              = var.kms_alias_prefix
    iam_permissions_boundary      = var.iam_permissions_boundary
    tags                          = var.tags
    atlantis_plan_role_arn        = var.atlantis_plan_role_arn
    atlantis_apply_role_arn       = var.atlantis_apply_role_arn
  }
}
