# Exports AWS cell topology records consolidating EKS endpoints, VPC subnets, and storage buckets.

output "record" {
  description = "Record of the AWS workload cell cluster."
  value       = module.interface.record
}

output "cluster_name" {
  description = "Name of the Kubernetes cluster."
  value       = local.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint."
  value       = local.cluster_endpoint
}

output "cluster_ca_certificate" {
  description = "Cluster CA certificate data."
  value       = local.cluster_ca_certificate
}

output "oidc_issuer_url" {
  description = "Cluster OIDC issuer URL for cross-cloud federation."
  value       = local.oidc_issuer_url
}

output "vpc_id" {
  description = "VPC ID of the cluster network."
  value       = local.vpc_id
}

output "bucket_names" {
  description = "Map of storage tier bucket names."
  value       = local.enable_storage ? { for k, b in module.storage[0].record.buckets : k => b.name } : {}
}

output "backups_bucket" {
  description = "Name of the backups S3 bucket."
  value       = local.enable_storage ? module.storage[0].record.buckets.backups.name : ""
}

output "karpenter_instance_profile_name" {
  description = "Name of the IAM instance profile used by Karpenter nodes."
  value       = "${var.iam_name_prefix}${var.cluster_name}-karpenter-node"
}

output "instance_profile_name" {
  description = "Name of the IAM instance profile used by nodes in the cluster."
  value       = "${var.iam_name_prefix}${var.cluster_name}-karpenter-node"
}

output "account_id" {
  description = "AWS account ID hosting the cell cluster."
  value       = data.aws_caller_identity.current.account_id
}

output "atlantis_plan_role_arn" {
  description = "IAM role ARN assumed by Atlantis during plan operations."
  value       = var.atlantis_plan_role_arn
}

output "atlantis_apply_role_arn" {
  description = "IAM role ARN assumed by Atlantis during apply operations."
  value       = var.atlantis_apply_role_arn
}
