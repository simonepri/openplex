# Exports GCP Secret Manager secret IDs, secret names, and replication locations.

output "record" {
  description = "Canonical secret record."
  value       = module.interface.record
}
