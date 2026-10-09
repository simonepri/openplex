# Exports AWS control plane topology records consolidating EKS endpoints, VPC subnets, and ECR repositories.

output "record" {
  description = "Record of the AWS control plane cluster."
  value       = module.interface.record
}

output "cluster_name" {
  description = "Name of the Kubernetes cluster."
  value       = module.cluster.record.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint."
  value       = module.cluster.record.endpoint
}

output "cluster_ca_certificate" {
  description = "Cluster CA certificate data."
  value       = module.cluster.record.ca_certificate
}

output "vpc_id" {
  description = "VPC ID of the cluster network."
  value       = module.vpc.record.vpc_id
}

output "dns_zone_id" {
  description = "Route 53 hosted zone ID for DNS subdomain delegation."
  value       = var.enable_dns && length(module.dns) > 0 ? module.dns[0].record.zone_id : null
}

output "workspace_publisher_role_arn" {
  description = "IAM role ARN for GitHub Actions workspace and infrastructure image publisher."
  value       = aws_iam_role.workspace_publisher.arn
}

output "cluster_annotations" {
  description = "Annotations applied to the control plane cluster registration."
  value       = local.ctrl_cluster_annotations
}

output "account_id" {
  description = "AWS account ID hosting the control plane cluster."
  value       = local.account_id
}

output "cloudtrail_log_group_arn" {
  description = "ARN of the CloudWatch log group for CloudTrail."
  value       = module.cloud_trail.log_group_arn
}

output "cloudtrail_log_group_name" {
  description = "Name of the CloudWatch log group for CloudTrail."
  value       = module.cloud_trail.log_group_name
}

output "cloudtrail_trail_arn" {
  description = "ARN of the CloudTrail trail."
  value       = module.cloud_trail.trail_arn
}

output "atlantis_plan_role_arn" {
  description = "IAM role ARN assumed by Atlantis during plan operations."
  value       = local.atlantis_plan_role_arn
}

output "atlantis_apply_role_arn" {
  description = "IAM role ARN assumed by Atlantis during apply operations."
  value       = local.atlantis_apply_role_arn
}
