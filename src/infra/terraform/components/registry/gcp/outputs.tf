# Exports Artifact Registry repository URLs, project locations, and package identifiers.

output "record" {
  description = "Canonical registry record shaped by the contract module."
  value       = module.interface.record
}
