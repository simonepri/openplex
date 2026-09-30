# Exports ECR repository URLs, registry IDs, and repository ARN identifiers.

output "record" {
  description = "Canonical registry record shaped by the contract module."
  value       = module.interface.record
}
