# Exports VPC network names, self links, subnetwork IDs, and internal CIDR ranges.

output "record" {
  description = "Canonical output record for the VPC component."
  value       = module.interface.record
}
