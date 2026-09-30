# Exports S3 bucket names, bucket ARNs, domain names, and KMS key ARNs.

output "record" {
  description = "Canonical storage record describing realized storage buckets."
  value       = module.interface.record
}

output "kms_key_arn" {
  description = "ARN of the KMS customer-managed key used for storage encryption."
  value       = aws_kms_key.storage.arn
}
