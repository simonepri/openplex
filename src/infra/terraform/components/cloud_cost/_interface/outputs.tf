# Exports canonical billing export dataset and IAM identity attributes for the cloud cost contract.

output "record" {
  description = "Canonical cloud_cost record containing billing export and IAM details."
  value       = local.record
}
