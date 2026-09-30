# Exports calculated VPC resource names and normalized network topology records.

output "names" {
  description = "Deterministic resource names computed from contract inputs."
  value       = local.names
}

output "record" {
  description = "Canonical output record for the VPC component."
  value       = local.record
}
