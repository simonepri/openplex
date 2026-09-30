# Computes deterministic storage tier bucket names and KMS keys from installation name parameters.

locals {
  names = {
    for tier in var.storage_tiers : tier => "${var.installation_name}-${var.cell_name}-${tier}"
  }
}
