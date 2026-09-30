# Exports endpoint records, kubeconfig contexts, and service account tokens for local deployments.

output "control_plane" {
  description = "Record of the local control plane cluster."
  value       = module.control_plane.record
}

output "cell" {
  description = "Record of the local workload cell cluster."
  value       = module.cell.record
}
