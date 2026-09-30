# Exports GCS bucket names, storage URLs, and KMS crypto key identifiers.

output "record" {
  description = "Canonical storage record describing realized storage buckets."
  value       = module.interface.record
}
