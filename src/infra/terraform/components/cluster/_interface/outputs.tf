# Exports deterministic resource names and normalized connection records for the cluster interface.

output "names" {
  description = "Deterministic resource names computed for the cluster."
  value       = local.names
}

output "record" {
  description = "Canonical output record for the cluster."
  value       = local.record
}
