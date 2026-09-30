# Exports repository name mappings and normalized container registry connection records.

output "names" {
  description = "Map of logical repository names to provider-specific repository names."
  value       = local.names
}

output "record" {
  description = "Canonical registry record shaped from realized provider outputs."
  value       = local.record
}
