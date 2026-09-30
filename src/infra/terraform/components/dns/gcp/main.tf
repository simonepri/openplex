# Provisions GCP Cloud DNS managed zones, DNSSEC configurations, and record sets.

module "interface" {
  source = "../_interface"

  domain_name    = var.domain_name
  is_cell        = var.is_cell
  parent_zone_id = var.parent_zone_id
  realized = {
    zone_id      = google_dns_managed_zone.this.name
    name_servers = google_dns_managed_zone.this.name_servers
  }
}

resource "google_dns_managed_zone" "this" {
  name     = replace(module.interface.names.domain, ".", "-")
  dns_name = "${module.interface.names.domain}."

  dnssec_config {
    state = "on"
  }
}
