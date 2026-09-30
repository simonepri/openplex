# Specifies required AWS provider versions and OpenTofu constraints for Route 53 DNS management.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.66.0"
    }
  }
}
