# Exports GCP control plane topology records consolidating GKE endpoints, VPC subnets, and registries.

output "record" {
  description = "Record of the GCP control plane cluster."
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
