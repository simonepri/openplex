# Exports EKS cluster endpoints, OIDC issuer URLs, certificate authority data, and security group IDs.

output "record" {
  description = "Canonical output record for the cluster."
  value       = module.interface.record
}

output "karpenter_interruption_queue_arn" {
  description = "ARN of the SQS queue used for Karpenter interruption handling."
  value       = try(aws_sqs_queue.karpenter_interruption[0].arn, null)
}

output "karpenter_interruption_queue_name" {
  description = "Name of the SQS queue used for Karpenter interruption handling."
  value       = try(aws_sqs_queue.karpenter_interruption[0].name, null)
}

output "karpenter_instance_profile_name" {
  description = "Name of the IAM instance profile used by Karpenter nodes."
  value       = aws_iam_instance_profile.karpenter.name
}

output "instance_profile_name" {
  description = "Name of the IAM instance profile used by Karpenter nodes."
  value       = aws_iam_instance_profile.karpenter.name
}
