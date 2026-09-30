# Exports GCP BigQuery billing dataset IDs and OpenCost service account identities.

output "record" {
  description = "Canonical cloud_cost record containing billing export and IAM details."
  value       = module.interface.record
}
