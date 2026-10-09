# Specifies required AWS, TLS, and OpenTofu provider constraints for AWS EKS cluster provisioning.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
  }
}
