# Exports DNS delegation attributes and Cloudflare zone information.

output "delegated_zones" {
  description = "List of delegated subdomains and their target nameservers."
  value       = local.enable_cloudflare ? local.delegations : {}
}

output "zone_id" {
  description = "Cloudflare zone ID for the managed apex domain."
  value       = try(data.cloudflare_zone.this[0].id, null)
}

output "hooks_cname" {
  description = "CNAME target hostname for hooks webhook ingress, if configured."
  value       = try(cloudflare_dns_record.hooks[0].name, null)
}
