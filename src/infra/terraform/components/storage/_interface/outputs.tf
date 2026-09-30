# Exports calculated storage bucket names and normalized object storage connection records.

output "names" {
  description = "Deterministic storage tier bucket names calculated from inputs."
  value       = local.names
}

output "record" {
  description = "Canonical storage record describing realized storage buckets."
  value       = local.record
}
