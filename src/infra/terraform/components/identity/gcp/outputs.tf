# Exports Google service account emails, unique IDs, and Workload Identity federation mappings.

output "record" {
  description = "Canonical identity record containing realized role ARNs."
  value       = module.interface.record
}
