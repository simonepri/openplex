# Specifies OpenTofu provider configurations, remote backend state, and provider version constraints for DNS delegations.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    key     = "dns/fleet.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token != null ? var.cloudflare_api_token : (local.enable_cloudflare ? null : "0000000000000000000000000000000000000000")
}
