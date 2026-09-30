# Normalizes canonical DNS output records including hosted zone IDs, name servers, and domain names.

locals {
  record = var.realized == null ? null : {
    zone_id        = var.realized.zone_id
    name_servers   = var.realized.name_servers
    domain_name    = var.domain_name
    is_cell        = var.is_cell
    parent_zone_id = var.parent_zone_id
  }
}
