# Exports EC2 Tailscale router instance IDs, private IPs, and security group identifiers.

output "record" {
  description = "Canonical output schema built from realized AWS resources."
  value       = module.interface.record
}

output "operator_oauth_secret_name" {
  description = "Name of the Secrets Manager secret containing operator OAuth credentials."
  value       = try(aws_secretsmanager_secret.operator_oauth[0].name, null)
}

output "operator_oauth_secret_arn" {
  description = "ARN of the Secrets Manager secret containing operator OAuth credentials."
  value       = try(aws_secretsmanager_secret.operator_oauth[0].arn, null)
}
