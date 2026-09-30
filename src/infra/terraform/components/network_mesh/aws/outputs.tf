# Exports EC2 Tailscale router instance IDs, private IPs, and security group identifiers.

output "record" {
  description = "Canonical output schema built from realized AWS resources."
  value       = module.interface.record
}
