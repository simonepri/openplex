# Exports calculated secret resource names and normalized secret manager output records.

output "names" {
  description = "Calculated resource names."
  value       = local.names
}

output "record" {
  description = "Canonical output record constructed from realized provider resources."
  value       = local.record
}
