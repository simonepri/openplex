# Specifies OpenTofu provider configurations, remote backend state, and provider version constraints.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.66.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "3.3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.2.1"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "0.29.2"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "4.0.6"
    }
  }

  backend "s3" {
    key     = "production/fleet.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      cost-center = "shared-infrastructure"
      environment = "production"
      managed-by  = "opentofu"
    }
  }
}

# provider "google" {
#   project = var.gcp_project
#   region  = var.gcp_region
#
#   default_labels = {
#     cost-center = "shared-infrastructure"
#     environment = "production"
#     managed-by  = "opentofu"
#   }
# }

provider "helm" {
  kubernetes = {
    host                   = module.control_plane.cluster_endpoint
    cluster_ca_certificate = base64decode(module.control_plane.cluster_ca_certificate)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.control_plane.cluster_name]
    }
  }
}

provider "kubernetes" {
  host                   = module.control_plane.cluster_endpoint
  cluster_ca_certificate = base64decode(module.control_plane.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.control_plane.cluster_name]
  }
}

# Stage 2: Cell Kubernetes Provider for adopted Phoenix HyperPod cluster
# provider "kubernetes" {
#   alias                  = "cell_aws"
#   host                   = module.cell_aws_usw2.cluster_endpoint
#   cluster_ca_certificate = base64decode(module.cell_aws_usw2.cluster_ca_certificate)
#   exec {
#     api_version = "client.authentication.k8s.io/v1beta1"
#     command     = "aws"
#     args        = ["eks", "get-token", "--cluster-name", module.cell_aws_usw2.cluster_name]
#   }
# }

# data "google_client_config" "current" {}
#
# provider "kubernetes" {
#   alias                  = "cell_gcp"
#   host                   = try(module.cell_gcp_euw4[0].cluster_endpoint, "https://127.0.0.1")
#   cluster_ca_certificate = try(base64decode(module.cell_gcp_euw4[0].cluster_ca_certificate), "")
#   token                  = try(data.google_client_config.current.access_token, "")
# }

provider "tailscale" {
  api_key             = var.tailscale_api_key != "" ? var.tailscale_api_key : null
  oauth_client_id     = var.tailscale_oauth_client_id != "" ? var.tailscale_oauth_client_id : null
  oauth_client_secret = var.tailscale_oauth_client_secret != "" ? var.tailscale_oauth_client_secret : null
  tailnet             = coalesce(var.tailnet_name, "unused")
}