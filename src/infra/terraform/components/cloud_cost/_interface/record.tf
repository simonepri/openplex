# Normalizes canonical cloud cost record schemas across AWS Cost and Usage Reports and GCP BigQuery billing.

locals {
  record = var.realized == null ? null : {
    cluster_name            = var.cluster_name
    cluster_oidc_issuer_url = var.cluster_oidc_issuer_url
    cluster_oidc_arn        = var.cluster_oidc_arn
    project_id              = var.project_id
    bucket_name             = try(var.realized.bucket_name, "")
    athena_database         = try(var.realized.athena_database, "")
    athena_workgroup        = try(var.realized.athena_workgroup, "")
    role_arn                = try(var.realized.role_arn, "")
    bigquery_dataset        = try(var.realized.bigquery_dataset, "")
  }
}
