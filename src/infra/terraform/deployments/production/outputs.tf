# Exports consolidated endpoint records, topology states, and cluster connection attributes for production.

output "control_plane" {
  description = "Record of the control plane cluster."
  value       = module.control_plane.record
}

output "cell_aws" {
  description = "Record of the AWS workload cell."
  # Stage 2: value = module.cell_aws_usw2.record
  value = null
}

output "cell_gcp" {
  description = "Record of the GCP workload cell if enabled."
  value       = null
}

output "federated_identity_gcp" {
  description = "Record of federated AWS identity roles for the GCP workload cell."
  value       = null
}

output "git_deploy_key_public" {
  description = "ED25519 public key of the Argo CD deploy key to add to GitHub repository settings."
  value       = tls_private_key.git_deploy_key.public_key_openssh
}

output "workspace_publisher_role_arn" {
  description = "IAM role ARN for GitHub Actions workspace and infrastructure image publisher."
  value       = module.control_plane.workspace_publisher_role_arn
}