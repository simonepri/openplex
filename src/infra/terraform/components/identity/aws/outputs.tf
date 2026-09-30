# Exports IAM role ARNs, role names, and Pod Identity association mappings for workload components.

output "record" {
  description = "Canonical identity record containing realized role ARNs."
  value       = module.interface.record
}
