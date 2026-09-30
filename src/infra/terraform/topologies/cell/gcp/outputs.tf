# Exports GCP cell topology records consolidating GKE endpoints, VPC subnets, and GCS buckets.

output "record" {
  description = "Record of the GCP workload cell cluster."
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

output "oidc_issuer_url" {
  description = "Cluster OIDC issuer URL for cross-cloud federation."
  value       = module.cluster.record.oidc_issuer_url
}

output "project_number" {
  description = "Numeric identifier of the GCP project hosting the cell."
  value       = data.google_project.current.number
}

output "project_id" {
  description = "GCP project ID hosting the cell."
  value       = data.google_project.current.project_id
}

output "name_servers" {
  description = "Name servers of the GCP cell Cloud DNS managed zone."
  value       = try(module.dns[0].record.name_servers, [])
}

output "dns_zone_id" {
  description = "Zone ID of the GCP cell Cloud DNS managed zone."
  value       = try(module.dns[0].record.zone_id, null)
}


