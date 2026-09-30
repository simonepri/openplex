# Computes deterministic hosted zone and delegation record names from input domain parameters.

locals {
  names = {
    domain = var.domain_name
  }
}
