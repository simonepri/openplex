# Normalizes canonical object storage records mapping storage tiers to bucket ARNs and endpoints.

locals {
  record = var.realized == null ? null : {
    buckets     = var.realized.buckets
    kms_key_arn = try(var.realized.kms_key_arn, null)
  }
}
