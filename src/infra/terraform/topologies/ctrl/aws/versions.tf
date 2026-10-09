# Specifies required AWS, random, and Tailscale provider versions and OpenTofu constraints for AWS control plane topologies.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.9.1"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "0.29.2"
    }
  }
}
