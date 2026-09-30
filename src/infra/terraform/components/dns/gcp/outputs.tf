# Exports Cloud DNS managed zone names, name servers, and domain specifications.

output "record" {
  description = "Canonical DNS zone record schema."
  value       = module.interface.record
}
