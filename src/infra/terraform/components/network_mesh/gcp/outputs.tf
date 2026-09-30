# Exports GCP Tailscale router instance IDs, internal IPs, and service account identifiers.

output "record" {
  description = "Canonical output schema built from realized GCP resources."
  value       = module.interface.record
}
