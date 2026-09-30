# Specifies required Google provider versions and OpenTofu constraints for GCP Secret Manager.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.4.0"
    }
  }
}
