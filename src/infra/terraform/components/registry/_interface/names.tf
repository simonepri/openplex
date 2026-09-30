# Computes deterministic container repository names from installation name and logical repository keys.

locals {
  names = {
    for r in var.repositories : r => "${var.installation_name}/${r}"
  }
}
