# Exports VPC IDs, CIDR blocks, subnet IDs categorized by tier, and NAT gateway IPs.

output "record" {
  description = "Canonical output record for the VPC component."
  value       = module.interface.record
}
