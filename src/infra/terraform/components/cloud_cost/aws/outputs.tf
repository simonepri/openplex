# Exports AWS Cost and Usage Report S3 bucket names, bucket ARNs, and KMS key ARNs.

output "record" {
  description = "Canonical cloud_cost record containing billing export and IAM details."
  value       = module.interface.record
}
