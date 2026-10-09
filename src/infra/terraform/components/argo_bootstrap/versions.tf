# Configures required OpenTofu providers including Helm and Kubernetes for Argo CD bootstrapping.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "3.3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.3.0"
    }
  }
}
