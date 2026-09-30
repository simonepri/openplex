# Specifies required Google provider versions and OpenTofu constraints for GCP VPC networking.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.4.0"
    }
  }
}
