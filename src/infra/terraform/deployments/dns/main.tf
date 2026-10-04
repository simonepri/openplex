# Provisions DNS NS delegation records in Cloudflare pointing to downstream DNS zones (e.g. AWS Route 53).

locals {
  deployment = yamldecode(file("${path.module}/deployment.yaml"))

  enable_cloudflare  = coalesce(var.enable_cloudflare, local.deployment.installation.enable_cloudflare)
  zone_name          = coalesce(var.zone_name, local.deployment.installation.zone_name)
  hooks_nlb_hostname = var.hooks_nlb_hostname != null ? var.hooks_nlb_hostname : try(local.deployment.installation.hooks_nlb_hostname, null)
  delegations        = coalesce(var.delegations, try(local.deployment.installation.delegations, {}))

  delegation_records = local.enable_cloudflare ? flatten([
    for subdomain, nameservers in local.delegations : [
      for ns in nameservers : {
        subdomain = subdomain
        name      = "${subdomain}.${local.zone_name}"
        ns        = ns
      }
    ]
  ]) : []
}

data "cloudflare_zone" "this" {
  count = local.enable_cloudflare ? 1 : 0

  filter = {
    name = local.zone_name
  }
}

resource "cloudflare_dns_record" "delegation" {
  for_each = {
    for r in local.delegation_records : "${r.subdomain}-${r.ns}" => r
  }

  zone_id = data.cloudflare_zone.this[0].id
  name    = "${each.value.subdomain}.${local.zone_name}"
  type    = "NS"
  content = each.value.ns
  ttl     = 300
}

moved {
  from = cloudflare_record.delegation
  to   = cloudflare_dns_record.delegation
}

resource "cloudflare_dns_record" "hooks" {
  count = local.enable_cloudflare && local.hooks_nlb_hostname != null && local.hooks_nlb_hostname != "" ? 1 : 0

  zone_id = data.cloudflare_zone.this[0].id
  name    = "hooks.${local.zone_name}"
  type    = "CNAME"
  content = local.hooks_nlb_hostname
  ttl     = 300
  proxied = false
}

moved {
  from = cloudflare_record.hooks
  to   = cloudflare_dns_record.hooks
}

