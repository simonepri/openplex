# Exports Route 53 hosted zone IDs, name servers, and fully qualified domain names.

output "record" {
  description = "Canonical DNS zone record schema."
  value       = module.interface.record
}
