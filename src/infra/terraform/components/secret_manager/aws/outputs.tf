# Exports Secrets Manager secret ARNs, secret names, and KMS key IDs.

output "record" {
  description = "Canonical secret record."
  value       = module.interface.record
}
