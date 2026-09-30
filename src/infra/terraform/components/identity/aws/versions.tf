# Specifies required AWS provider versions and OpenTofu constraints for IAM identity management.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.66.0"
    }
  }
}
