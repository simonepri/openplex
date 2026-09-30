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
