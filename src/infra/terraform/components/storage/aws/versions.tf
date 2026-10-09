# Specifies required AWS provider versions and OpenTofu constraints for S3 object storage.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
  }
}
