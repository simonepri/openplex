# Exports AWS CloudTrail, CloudWatch log group, S3 bucket, and KMS key ARNs.

output "log_group_arn" {
  description = "ARN of the CloudWatch log group."
  value       = aws_cloudwatch_log_group.this.arn
}

output "log_group_name" {
  description = "Name of the CloudWatch log group."
  value       = aws_cloudwatch_log_group.this.name
}

output "trail_arn" {
  description = "ARN of the CloudTrail trail."
  value       = aws_cloudtrail.this.arn
}

output "s3_bucket_arn" {
  description = "ARN of the CloudTrail S3 archive bucket."
  value       = aws_s3_bucket.this.arn
}

output "kms_key_arn" {
  description = "ARN of the KMS customer-managed key."
  value       = aws_kms_key.this.arn
}
