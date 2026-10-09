# Computes deterministic container repository names from the cluster name and logical repository keys.

locals {
  namespace = var.cluster_name
  names = {
    for r in var.repositories : r => "${local.namespace}/${r}"
  }
}
