# Tests Route 53 public and cell hosted zone creation and delegation in the AWS DNS component.

mock_provider "aws" {
  mock_resource "aws_route53_zone" {
    defaults = {
      zone_id      = "Z1234567890ABC"
      name_servers = ["ns-1.awsdns-01.org", "ns-2.awsdns-02.co.uk"]
    }
  }

  mock_resource "aws_route53_record" {
    defaults = {}
  }
}

variables {
  domain_name = "example.com"
}

run "verifies_public_hosted_zone" {
  command = plan

  assert {
    condition     = aws_route53_zone.this.name == "example.com"
    error_message = "Route 53 hosted zone name must match domain_name."
  }

  assert {
    condition     = length(aws_route53_record.this) == 0
    error_message = "NS delegation record must not be created for apex non-cell zone."
  }

  assert {
    condition     = output.record.domain_name == "example.com"
    error_message = "Output record must contain domain_name."
  }
}

run "verifies_cell_hosted_zone_with_delegation" {
  command = plan

  variables {
    domain_name    = "cell-1.example.com"
    is_cell        = true
    parent_zone_id = "ZPARENTZONEID"
  }

  assert {
    condition     = length(aws_route53_record.this) == 1
    error_message = "NS delegation record must be created when is_cell is true and parent_zone_id is set."
  }

  assert {
    condition     = aws_route53_record.this[0].zone_id == "ZPARENTZONEID"
    error_message = "NS delegation record must target parent_zone_id."
  }
}
