# Normalizes canonical object storage records mapping storage tiers to bucket ARNs and endpoints.

locals {
  record = var.realized == null ? null : {
    buckets = var.realized.buckets
  }
}
