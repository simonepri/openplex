# Exports consolidated endpoint records, topology states, and cluster connection attributes for production.

output "control_plane" {
  description = "Record of the control plane cluster."
  value       = module.control_plane.record
}

output "cell_aws" {
  description = "Record of the AWS workload cell."
  value       = module.cell_aws_usw2.record
}

output "cell_gcp" {
  description = "Record of the GCP workload cell if enabled."
  value       = try(module.cell_gcp_euw4[0].record, null)
}

output "federated_identity_gcp" {
  description = "Record of federated AWS identity roles for the GCP workload cell."
  value       = try(module.federated_identity_gcp[0].record, null)
}

