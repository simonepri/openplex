# Computes deterministic VPC, subnet, gateway, and routing table resource names from network parameters.

locals {
  names = {
    vpc              = var.name
    internet_gateway = "${var.name}-igw"
    router           = "${var.name}-router"
    router_nat       = "${var.name}-nat"
    subnets = {
      private = [for idx, _ in coalesce(var.tier_subnets.private, []) : "${var.name}-private-${idx}"]
      public  = [for idx, _ in coalesce(var.tier_subnets.public, []) : "${var.name}-public-${idx}"]
      pod     = [for idx, _ in coalesce(var.tier_subnets.pod, []) : "${var.name}-pod-${idx}"]
    }
  }
}
