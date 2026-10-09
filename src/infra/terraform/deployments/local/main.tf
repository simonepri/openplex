# Configures local OpenTofu deployment connecting local Kubernetes clusters, Argo CD, and service accounts.

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "3.3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.3.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "4.4.1"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "0.29.2"
    }
  }

  backend "local" {
    path = ".terraform/local.tfstate"
  }
}

locals {
  deployment = try(
    yamldecode(file("${path.module}/deployment.yaml")),
    {
      installation = {
        public_domain   = "local.internal"
        intranet_domain = "corp.local.internal"
        cluster_domain  = "c.corp.local.internal"
        git = {
          url             = "git://172.19.255.21:9418/openplex.git"
          target_revision = "main"
        }
      }
      clusters = {
        ctrl-eaws-lh1 = {
          network = { service_cidr = "172.16.0.0/20" }
        }
        cell-eaws-lh1 = {}
      }
    }
  )
  ctrl_cluster             = local.deployment.clusters["ctrl-eaws-lh1"]
  ctrl_cluster_name        = "${var.name_prefix}${try(local.ctrl_cluster.name, "ctrl-eaws-lh1")}"
  ctrl_gateway_ipv4        = cidrhost(local.ctrl_cluster.network.service_cidr, 11)
  cell_cluster             = local.deployment.clusters["cell-eaws-lh1"]
  cell_cluster_name        = "${var.name_prefix}${try(local.cell_cluster.name, "cell-eaws-lh1")}"
  public_domain            = local.deployment.installation.public_domain
  intranet_domain          = try(local.deployment.installation.intranet_domain, "corp.${local.public_domain}")
  cluster_domain           = try(local.deployment.installation.cluster_domain, "c.${local.intranet_domain}")
  floci_kubernetes_version = "1.36"
  floci_eks_token_args = [
    "exec", "python", "--", "python3",
    abspath("${path.module}/../../../tools/cloud_emulator/auth/eks_token.py"),
    "--endpoint-url", var.floci_endpoint,
    "--region", "us-west-2",
  ]
  team_definition_files = fileset("${path.module}/../../../definitions/teams", "*.yaml")
  teams = toset([
    for f in local.team_definition_files :
    yamldecode(file("${path.module}/../../../definitions/teams/${f}")).slug
  ])
}

data "aws_caller_identity" "current" {}

provider "aws" {
  region                      = "us-west-2"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  default_tags {
    tags = var.tags
  }

  endpoints {
    cloudwatchlogs = var.floci_endpoint
    ec2            = var.floci_endpoint
    ecr            = var.floci_endpoint
    eks            = var.floci_endpoint
    iam            = var.floci_endpoint
    kms            = var.floci_endpoint
    route53        = var.floci_endpoint
    s3             = var.floci_endpoint
    secretsmanager = var.floci_endpoint
    sqs            = var.floci_endpoint
    sts            = var.floci_endpoint
  }
}

provider "helm" {
  kubernetes = {
    host                   = module.control_plane.cluster_endpoint
    cluster_ca_certificate = base64decode(module.control_plane.cluster_ca_certificate)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "mise"
      args        = concat(local.floci_eks_token_args, ["--cluster-name", module.control_plane.cluster_name])
    }
  }
}

provider "kubernetes" {
  host                   = module.control_plane.cluster_endpoint
  cluster_ca_certificate = base64decode(module.control_plane.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "mise"
    args        = concat(local.floci_eks_token_args, ["--cluster-name", module.control_plane.cluster_name])
  }
}

provider "kubernetes" {
  alias                  = "control_plane"
  host                   = module.control_plane.cluster_endpoint
  cluster_ca_certificate = base64decode(module.control_plane.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "mise"
    args        = concat(local.floci_eks_token_args, ["--cluster-name", module.control_plane.cluster_name])
  }
}

provider "kubernetes" {
  alias                  = "cell"
  host                   = module.cell.cluster_endpoint
  cluster_ca_certificate = base64decode(module.cell.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "mise"
    args        = concat(local.floci_eks_token_args, ["--cluster-name", module.cell.cluster_name])
  }
}

provider "tailscale" {
  api_key = "dummy"
  tailnet = "dummy"
}

resource "kubernetes_service_account_v1" "argocd_manager" {
  provider = kubernetes.cell

  metadata {
    name      = "argocd-manager"
    namespace = "kube-system"
  }
}

resource "kubernetes_cluster_role_binding_v1" "argocd_manager" {
  provider = kubernetes.cell

  metadata {
    name = "argocd-manager-binding"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin"
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argocd_manager.metadata[0].name
    namespace = "kube-system"
  }
}

resource "kubernetes_token_request_v1" "argocd_manager" {
  provider = kubernetes.cell

  metadata {
    name      = kubernetes_service_account_v1.argocd_manager.metadata[0].name
    namespace = "kube-system"
  }

  spec {
    expiration_seconds = 31536000
  }
}

resource "tls_private_key" "coder_provisioner" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "coder_provisioner" {
  private_key_pem = tls_private_key.coder_provisioner.private_key_pem

  subject {
    common_name  = "cluster:coder-provisioner:${module.cell.cluster_name}"
    organization = "cluster:coder-provisioners"
  }
}

resource "kubernetes_certificate_signing_request_v1" "coder_provisioner" {
  provider = kubernetes.cell

  metadata {
    name = "coder-provisioner-${substr(sha256(module.cell.cluster_ca_certificate), 0, 12)}"
  }

  spec {
    request            = tls_cert_request.coder_provisioner.cert_request_pem
    signer_name        = "kubernetes.io/kube-apiserver-client"
    expiration_seconds = 31536000
    usages             = ["client auth", "digital signature"]
  }

  auto_approve = true
}

resource "kubernetes_secret_v1" "coder_cell_kubeconfig" {
  provider = kubernetes.control_plane

  # The ExternalSecret projects this bootstrap credential into the Argo-managed
  # coder namespace. Kubernetes signs a cell-scoped client identity at apply time.
  metadata {
    name      = "coder-cell-kubeconfig"
    namespace = "kube-system"
    labels = {
      "app.kubernetes.io/component"  = "provisioner"
      "app.kubernetes.io/managed-by" = "opentofu"
      "app.kubernetes.io/part-of"    = "coder"
    }
  }

  data = {
    kubeconfig = yamlencode({
      apiVersion = "v1"
      kind       = "Config"
      clusters = [{
        name = module.cell.cluster_name
        cluster = {
          certificate-authority-data = module.cell.cluster_ca_certificate
          server                     = "https://${local.cell_cluster.network.node_ipv4}:6443"
        }
      }]
      contexts = [{
        name = module.cell.cluster_name
        context = {
          cluster = module.cell.cluster_name
          user    = tls_cert_request.coder_provisioner.subject[0].common_name
        }
      }]
      current-context = module.cell.cluster_name
      users = [{
        name = tls_cert_request.coder_provisioner.subject[0].common_name
        user = {
          client-certificate-data = base64encode(kubernetes_certificate_signing_request_v1.coder_provisioner.certificate)
          client-key-data         = base64encode(tls_private_key.coder_provisioner.private_key_pem)
        }
      }]
    })
  }
}

data "kubernetes_config_map_v1" "ctrl_published_ca" {
  metadata {
    name      = "cluster-published-ca"
    namespace = "cert-manager-system"
  }
}

data "kubernetes_config_map_v1" "cell_published_ca" {
  provider = kubernetes.cell

  metadata {
    name      = "cluster-published-ca"
    namespace = "cert-manager-system"
  }
}

module "control_plane" {
  source = "../../topologies/ctrl/aws"

  cluster_name                  = local.ctrl_cluster_name
  vpc_cidr                      = local.ctrl_cluster.vpc_cidr
  availability_zones            = local.ctrl_cluster.availability_zones
  tier_subnets                  = local.ctrl_cluster.tier_subnets
  kubernetes_version            = local.floci_kubernetes_version
  enable_kms_secrets_encryption = true
  enable_control_plane_logging  = true
  enable_access_config          = true
  enable_addons                 = true
  enable_ebs_csi                = true
  enable_identity               = true
  enable_dns                    = true
  enable_network_mesh           = false
  cluster_provider              = "floci"
  cluster_environment           = "local"
  domain_name                   = local.intranet_domain
  intranet_domain_name          = local.intranet_domain
  public_domain_name            = local.public_domain
  cluster_domain_name           = local.cluster_domain
  oidc_tls_insecure_skip_verify = true
  git_repo_url                  = coalesce(var.git_repo_url, local.deployment.installation.git.url)
  target_revision               = "HEAD"
  fleet_availability            = var.fleet_availability
  public_access_cidrs           = []
  service_ipv4_cidr             = local.ctrl_cluster.network.service_cidr
  annotations = {
    # keep-sorted start
    "${module.cell.cluster_name}-cluster-ca" = data.kubernetes_config_map_v1.cell_published_ca.data["ca.crt"]
    "bucket-suffix"                          = data.aws_caller_identity.current.account_id
    "control-gateway-ipv4"                   = local.ctrl_gateway_ipv4
    "git-repo-url"                           = var.git_identity_url
    "intranet-domain"                        = local.intranet_domain
    "public-domain"                          = local.public_domain
    "resource-tags"                          = jsonencode(var.tags)
    "s3-endpoint"                            = "http://172.19.0.2:4566"
    "secret-store"                           = "local-secret-records"
    "service-cidr"                           = local.ctrl_cluster.network.service_cidr
    "storage-endpoint"                       = "http://172.19.0.2:4566"
    # keep-sorted end
  }

  registered_cells = [
    {
      name           = module.cell.cluster_name
      endpoint       = "https://${local.cell_cluster.network.node_ipv4}:6443"
      ca_certificate = module.cell.cluster_ca_certificate
      provider       = "floci"
      environment    = "local"
      labels         = local.cell_cluster.labels
      token          = kubernetes_token_request_v1.argocd_manager.token
      annotations = {
        # keep-sorted start
        "aws-account-id"               = data.aws_caller_identity.current.account_id
        "backups-bucket"               = module.cell.record.backups_bucket
        "bucket-suffix"                = data.aws_caller_identity.current.account_id
        "control-cluster-ca"           = data.kubernetes_config_map_v1.ctrl_published_ca.data["ca.crt"]
        "control-gateway-ipv4"         = local.ctrl_gateway_ipv4
        "global-storage-bucket-prefix" = "${local.ctrl_cluster_name}-global"
        "global-storage-bucket-suffix" = data.aws_caller_identity.current.account_id
        "global-storage-endpoint"      = "http://172.19.0.2:4566"
        "global-storage-provider"      = "Other"
        "global-storage-teams"         = join(",", sort(local.teams))
        "resource-tags"                = jsonencode(var.tags)
        "s3-endpoint"                  = "http://172.19.0.2:4566"
        "secret-store"                 = "local-secret-records"
        "service-cidr"                 = local.cell_cluster.network.service_cidr
        "storage-endpoint"             = "http://172.19.0.2:4566"
        # keep-sorted end
      }
    }
  ]

  iam_name_prefix          = var.iam_name_prefix
  kms_alias_prefix         = var.kms_alias_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
  tags                     = var.tags
}

module "cell" {
  source = "../../topologies/cell/aws"

  cluster_name                  = local.cell_cluster_name
  vpc_cidr                      = local.cell_cluster.vpc_cidr
  availability_zones            = local.cell_cluster.availability_zones
  tier_subnets                  = local.cell_cluster.tier_subnets
  kubernetes_version            = local.floci_kubernetes_version
  enable_kms_secrets_encryption = true
  enable_control_plane_logging  = true
  enable_access_config          = true
  enable_addons                 = true
  enable_ebs_csi                = true
  enable_identity               = true
  enable_dns                    = true
  enable_network_mesh           = false
  domain_name                   = local.intranet_domain
  fleet_availability            = "standalone"
  public_access_cidrs           = []
  service_ipv4_cidr             = local.cell_cluster.network.service_cidr

  iam_name_prefix          = var.iam_name_prefix
  kms_alias_prefix         = var.kms_alias_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
  tags                     = var.tags
}

resource "aws_s3_bucket" "global" {
  for_each = local.teams

  bucket = "${local.ctrl_cluster_name}-global-${each.value}-${data.aws_caller_identity.current.account_id}"
}
