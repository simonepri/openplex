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


