# Computes deterministic IAM role and service account names from cluster name and role keys.

locals {
  names = {
    for k, v in var.roles : k => "${var.cluster_name}-${k}"
  }
}
