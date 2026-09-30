# Exports S3 bucket names, bucket ARNs, domain names, and KMS key ARNs.

output "record" {
  description = "Canonical storage record describing realized storage buckets."
  value       = module.interface.record
}
