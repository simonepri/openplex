# Exports calculated IAM role names and normalized identity binding records.

output "names" {
  description = "Deterministic role names computed from the cluster name and role keys."
  value       = local.names
}

output "record" {
  description = "Canonical identity record containing provider-realized role ARNs."
  value       = local.record
}
