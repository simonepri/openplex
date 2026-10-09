# Specifies required Terraform version constraints and provider dependencies for the dev workspace template.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    coder = {
      source  = "registry.terraform.io/coder/coder"
      version = "2.19.0"
    }
    external = {
      source  = "registry.terraform.io/hashicorp/external"
      version = "2.4.2"
    }
    kubernetes = {
      source  = "registry.terraform.io/hashicorp/kubernetes"
      version = "3.3.0"
    }
  }
}

provider "coder" {}

# The OSS built-in provisioner reads the selected cell's keyless or local
# namespace-scoped kubeconfig from its read-only mount.
provider "kubernetes" {
  config_path    = var.kubernetes_config_path
  config_context = data.coder_parameter.cluster.value
}
