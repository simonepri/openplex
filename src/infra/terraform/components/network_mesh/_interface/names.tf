# Computes deterministic Tailscale router VM, security group, and secret names from input parameters.

locals {
  names = {
    instance = "${var.name}-mesh-router"
  }
}
