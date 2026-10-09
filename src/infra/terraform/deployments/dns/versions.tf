# Specifies OpenTofu provider configurations, remote backend state, and provider version constraints for DNS delegations.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.27.0"
    }
  }

  backend "s3" {
    # LINT.IfChange(opentofu-state-bucket)
    bucket = "openplex-platform-opentofu-state-400920695547"
    # LINT.ThenChange(//src/infra/terraform/deployments/research/main.tf:opentofu-state-bucket,//src/infra/terraform/deployments/research/versions.tf:opentofu-state-bucket)
    key     = "dns/fleet.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token != null ? var.cloudflare_api_token : (local.enable_cloudflare ? null : "0000000000000000000000000000000000000000")
}
