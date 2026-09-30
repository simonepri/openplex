# Exports calculated DNS hosted zone resource names and normalized delegation records.

output "names" {
  description = "Calculated resource naming attributes."
  value       = local.names
}

output "record" {
  description = "Canonical DNS zone record schema."
  value       = local.record
}
