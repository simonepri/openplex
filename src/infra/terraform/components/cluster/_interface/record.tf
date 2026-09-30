# Normalizes canonical cluster output records including endpoint URLs, OIDC issuers, and CA certificates.

locals {
  record = var.realized == null ? null : {
    cluster_name                  = var.realized.cluster_name
    endpoint                      = var.realized.endpoint
    ca_certificate                = var.realized.ca_certificate
    oidc_issuer_url               = var.realized.oidc_issuer_url
    oidc_provider_arn             = var.realized.oidc_provider_arn
    vpc_id                        = var.vpc_id
    subnet_ids                    = var.subnet_ids
    kubernetes_version            = var.kubernetes_version
    service_ipv4_cidr             = var.service_ipv4_cidr
    enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
    enable_control_plane_logging  = var.enable_control_plane_logging
  }
}
