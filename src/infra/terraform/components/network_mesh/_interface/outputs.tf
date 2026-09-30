# Exports calculated network mesh router resource names and normalized connection records.

output "names" {
  description = "Deterministic resource names computed from contract inputs."
  value       = local.names
}

output "record" {
  description = "Canonical output schema built from realized provider resources."
  value       = local.record
}
