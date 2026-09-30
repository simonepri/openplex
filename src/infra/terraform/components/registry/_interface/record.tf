# Normalizes canonical container registry records including repository URLs and registry endpoints.

locals {
  record = var.realized == null ? null : {
    registry_url = var.realized.registry_url
    repositories = var.realized.repositories
  }
}
