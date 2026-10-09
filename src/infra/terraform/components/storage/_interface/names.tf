# Computes deterministic storage tier bucket names and KMS keys from cluster name and account ID.

locals {
  names = {
    for tier in var.storage_tiers : tier => "${var.cluster_name}-${tier}-${var.account_id}"
  }
}
