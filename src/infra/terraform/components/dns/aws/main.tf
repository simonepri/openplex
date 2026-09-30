# Provisions AWS Route 53 DNS hosted zones, delegation records, and record sets.

module "interface" {
  source = "../_interface"

  domain_name    = var.domain_name
  is_cell        = var.is_cell
  parent_zone_id = var.parent_zone_id
  realized = {
    zone_id      = aws_route53_zone.this.zone_id
    name_servers = aws_route53_zone.this.name_servers
  }
}

resource "aws_route53_zone" "this" {
  name = module.interface.names.domain
}

resource "aws_route53_record" "this" {
  count = var.is_cell && var.parent_zone_id != "" ? 1 : 0

  zone_id = var.parent_zone_id
  name    = module.interface.names.domain
  type    = "NS"
  ttl     = 300
  records = aws_route53_zone.this.name_servers
}
