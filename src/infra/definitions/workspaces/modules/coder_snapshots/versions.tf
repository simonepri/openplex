# Declares required Terraform version constraints and provider requirements for the coder_snapshots module.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    coder = {
      source  = "coder/coder"
      version = "2.19.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.3.0"
    }
  }
}
