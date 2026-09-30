# Computes deterministic secret and KMS key resource names from installation and secret name parameters.

locals {
  names = {
    secret = var.secret_name
  }
}
