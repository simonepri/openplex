# Configures required OpenTofu providers including Cloudflare for R2 storage provisioning.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.27.0"
    }
  }
}
