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

output "project_number" {
  description = "Numeric identifier of the GCP project hosting the control plane."
  value       = data.google_project.current.number
}

output "project_id" {
  description = "GCP project ID hosting the control plane."
  value       = data.google_project.current.project_id
}

output "telemetry_pubsub_topic" {
  description = "Pub/Sub topic ID for cloud telemetry ingestion."
  value       = google_pubsub_topic.telemetry.id
}

output "telemetry_pubsub_subscription" {
  description = "Pub/Sub subscription ID for cloud telemetry ingestion."
  value       = google_pubsub_subscription.telemetry.id
}

