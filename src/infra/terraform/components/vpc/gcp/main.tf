# Provisions GCP VPC networks, custom subnetworks, Cloud Routers, NAT gateways, and firewall rules.

module "interface" {
  source             = "../_interface"
  name               = var.name
  cidr_block         = var.cidr_block
  availability_zones = var.availability_zones
  tier_subnets       = var.tier_subnets
  enable_flow_logs   = var.enable_flow_logs
  realized = {
    vpc_id             = google_compute_network.this.id
    private_subnet_ids = [for s in google_compute_subnetwork.this : s.id]
    public_subnet_ids  = []
    pod_subnet_ids     = flatten([for s in google_compute_subnetwork.this : [for r in s.secondary_ip_range : r.range_name]])
    nat_gateway_ips    = []
  }
}

resource "google_compute_network" "this" {
  name                    = module.interface.names.vpc
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "this" {
  count                    = length(coalesce(var.tier_subnets.private, [])) > 0 ? length(var.tier_subnets.private) : 1
  name                     = length(coalesce(var.tier_subnets.private, [])) > 0 ? module.interface.names.subnets.private[count.index] : "${module.interface.names.vpc}-subnet"
  network                  = google_compute_network.this.id
  ip_cidr_range            = length(coalesce(var.tier_subnets.private, [])) > 0 ? var.tier_subnets.private[count.index] : var.cidr_block
  private_ip_google_access = true

  dynamic "secondary_ip_range" {
    for_each = count.index == 0 ? coalesce(var.tier_subnets.pod, []) : []
    content {
      range_name    = length(var.tier_subnets.pod) == 1 ? "pods" : "pods-${secondary_ip_range.key}"
      ip_cidr_range = secondary_ip_range.value
    }
  }

  dynamic "secondary_ip_range" {
    for_each = count.index == 0 ? [1] : []
    content {
      range_name    = "services"
      ip_cidr_range = var.service_cidr
    }
  }

  dynamic "log_config" {
    for_each = var.enable_flow_logs ? [1] : []
    content {
      aggregation_interval = "INTERVAL_10_MIN"
      flow_sampling        = 0.5
      metadata             = "INCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_firewall" "internal_mesh" {
  name    = "${module.interface.names.vpc}-internal-mesh"
  network = google_compute_network.this.id

  allow {
    protocol = "udp"
    ports    = ["41641"]
  }

  allow {
    protocol = "tcp"
  }

  allow {
    protocol = "udp"
  }

  source_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
}

resource "google_compute_router" "this" {
  name    = module.interface.names.router
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  name                               = module.interface.names.router_nat
  router                             = google_compute_router.this.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}
