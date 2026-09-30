# Computes deterministic cluster, KMS key, and security group resource names from input parameters.

locals {
  names = {
    cluster = var.cluster_name
  }
}
