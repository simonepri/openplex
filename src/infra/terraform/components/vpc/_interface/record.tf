# Normalizes canonical VPC network output records including VPC IDs, subnet tiers, and CIDR allocations.

locals {
  record = var.realized == null ? null : {
    vpc_id             = var.realized.vpc_id
    private_subnet_ids = var.realized.private_subnet_ids
    public_subnet_ids  = var.realized.public_subnet_ids
    pod_subnet_ids     = var.realized.pod_subnet_ids
    nat_gateway_ips    = var.realized.nat_gateway_ips
    cidr_block         = var.cidr_block
    availability_zones = var.availability_zones
    enable_flow_logs   = var.enable_flow_logs
  }
}
