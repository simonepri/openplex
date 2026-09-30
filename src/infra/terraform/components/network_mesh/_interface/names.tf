# Computes deterministic Tailscale router VM, security group, and secret names from input parameters.

locals {
  names = {
    instance       = "${var.name}-mesh-router"
    operator_oauth = "${var.name}-tailscale-operator-oauth"
  }
}
