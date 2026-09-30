# Exports GKE cluster endpoints, CA certificates, and Workload Identity federation parameters.

output "record" {
  description = "Canonical output record for the cluster."
  value       = module.interface.record
}
