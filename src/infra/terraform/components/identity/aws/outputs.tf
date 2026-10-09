# Exports IAM role ARNs, role names, and Pod Identity association mappings for workload components.

output "atlantis_apply_role_arn" {
  description = "ARN of the IAM role assumed by Atlantis for running terraform apply."
  value       = local.atlantis_apply_role_arn
}

output "atlantis_plan_role_arn" {
  description = "ARN of the IAM role assumed by Atlantis for running terraform plan."
  value       = local.atlantis_plan_role_arn
}

output "record" {
  description = "Canonical identity record containing realized role ARNs."
  value       = module.interface.record
}
