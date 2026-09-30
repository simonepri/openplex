# Normalizes canonical identity records mapping Kubernetes service accounts to cloud IAM roles.

locals {
  record = var.realized == null ? null : {
    role_arns               = var.realized.role_arns
    cluster_oidc_issuer_url = var.cluster_oidc_issuer_url
    cluster_oidc_arn        = var.cluster_oidc_arn
    project_id              = var.project_id
    trust_mode              = var.trust_mode
  }
}

