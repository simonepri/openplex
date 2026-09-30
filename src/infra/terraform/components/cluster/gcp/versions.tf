# Specifies required Google, TLS, and OpenTofu provider constraints for GCP GKE cluster provisioning.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.4.0"
    }
  }
}
