# Exports EKS cluster endpoints, OIDC issuer URLs, certificate authority data, and security group IDs.

output "record" {
  description = "Canonical output record for the cluster."
  value       = module.interface.record
}
