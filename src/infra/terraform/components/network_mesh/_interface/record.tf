# Normalizes canonical network mesh router output records including instance IDs and advertised routes.

locals {
  record = var.realized == null ? null : {
    instance_id         = var.realized.instance_id
    private_ip          = var.realized.private_ip
    vpc_id              = var.vpc_id
    subnet_id           = var.subnet_id
    advertised_routes   = var.advertised_routes
    auth_key_configured = length(var.tailnet_auth_key) > 0
  }
}
