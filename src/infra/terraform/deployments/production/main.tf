# Deploys production infrastructure across control plane and cell topologies on AWS and GCP.

locals {
  deployment = try(
    yamldecode(file("${path.module}/deployment.yaml")),
    {
      installation = {
        public_domain   = "openplex.org"
        intranet_domain = "corp.openplex.org"
        cluster_domain  = "c.corp.openplex.org"
        git             = { url = "https://github.com/simonepri/openplex.git" }
      }
      clusters = {
        ctrl-aws-usw2 = {
          name               = "ctrl-aws-usw2"
          vpc_cidr           = "10.0.0.0/16"
          availability_zones = ["us-west-2a", "us-west-2b", "us-west-2c"]
          tier_subnets = {
            private = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
            public  = ["10.0.10.0/24", "10.0.11.0/24", "10.0.12.0/24"]
            pod     = ["10.0.64.0/18", "10.0.128.0/18", "10.0.192.0/18"]
          }
          network = { service_cidr = "172.16.0.0/20" }
        }
        cell-aws-usw2 = {
          name               = "cell-aws-usw2"
          vpc_cidr           = "10.1.0.0/16"
          availability_zones = ["us-west-2a", "us-west-2b", "us-west-2c"]
          tier_subnets = {
            private = ["10.1.1.0/24", "10.1.2.0/24", "10.1.3.0/24"]
            public  = ["10.1.10.0/24", "10.1.11.0/24", "10.1.12.0/24"]
            pod     = ["10.1.64.0/18", "10.1.128.0/18", "10.1.192.0/18"]
          }
          network = { service_cidr = "172.17.0.0/20" }
          labels  = {}
        }
      }
    }
  )
  ctrl_cluster      = local.deployment.clusters["ctrl-aws-usw2"]
  ctrl_gateway_ipv4 = cidrhost(local.ctrl_cluster.network.service_cidr, 11)
  # Stage 2: cell_aws          = local.deployment.clusters["cell-aws-usw2"]
  public_domain   = coalesce(var.public_domain, local.deployment.installation.public_domain)
  intranet_domain = try(local.deployment.installation.intranet_domain, "corp.${local.public_domain}")
  cluster_domain  = try(local.deployment.installation.cluster_domain, "c.${local.intranet_domain}")
  git_repo_url    = coalesce(var.git_repo_url, local.deployment.installation.git.url)
}

# data "aws_caller_identity" "current" {}

resource "tls_private_key" "git_deploy_key" {
  algorithm = "ED25519"
}

resource "aws_secretsmanager_secret" "git_deploy_key" {
  name                    = "${local.ctrl_cluster.name}-argocd-git-deploy-key"
  recovery_window_in_days = 0

  tags = {
    Name = "${local.ctrl_cluster.name}-argocd-git-deploy-key"
  }
}

resource "aws_secretsmanager_secret_version" "git_deploy_key" {
  secret_id     = aws_secretsmanager_secret.git_deploy_key.id
  secret_string = tls_private_key.git_deploy_key.private_key_openssh
}

module "control_plane" {
  source = "../../topologies/ctrl/aws"

  cluster_name                  = local.ctrl_cluster.name
  vpc_cidr                      = local.ctrl_cluster.vpc_cidr
  availability_zones            = local.ctrl_cluster.availability_zones
  tier_subnets                  = local.ctrl_cluster.tier_subnets
  kubernetes_version            = "1.36"
  enable_kms_secrets_encryption = true
  enable_flow_logs              = true
  enable_control_plane_logging  = true
  enable_cloud_cost             = var.enable_cloud_cost
  resource_prefix               = var.resource_prefix
  system_instance_types         = var.system_instance_types
  domain_name                   = local.intranet_domain
  intranet_domain_name          = local.intranet_domain
  public_domain_name            = local.public_domain
  cluster_domain_name           = local.cluster_domain
  access_domain_name            = local.cluster_domain
  tailnet_auth_key              = var.tailnet_auth_key
  manage_tailscale_acl          = var.manage_tailscale_acl
  git_repo_url                  = local.git_repo_url
  git_ssh_private_key           = tls_private_key.git_deploy_key.private_key_openssh
  git_repo_creds_url            = "git@github.com:simonepri"
  enable_git_repo_creds         = true
  target_revision               = var.target_revision
  fleet_availability            = "resilient"
  service_ipv4_cidr             = local.ctrl_cluster.network.service_cidr
  cluster_labels = {
    "acme-dns01" = "enabled"
  }
  annotations = {
    "aws-region"           = var.aws_region
    "aws-vpc-id"           = module.control_plane.vpc_id
    "control-gateway-ipv4" = local.ctrl_gateway_ipv4
    "hosted-domains"       = join(",", try(local.deployment.installation.hosted_domains, [local.public_domain]))
    "installation"         = local.deployment.installation.name
    "intranet-domain"      = local.intranet_domain
    "operator-emails"      = join(",", try(local.deployment.installation.operator_emails, []))
    "operator-group-email" = try(local.deployment.installation.operator_group_email, "operators@${local.public_domain}")
    "public-domain"        = local.public_domain
    "registered-cells"     = join(",", sort([for name, c in local.deployment.clusters : name if try(c.role, "") == "cell"]))
    "resource-prefix"      = var.resource_prefix
    "s3-endpoint"          = ""
    "storage-endpoint"     = "s3.${var.aws_region}.amazonaws.com"
    "secret-store"         = "runtime-secrets"
    "service-cidr"         = local.ctrl_cluster.network.service_cidr
    "tailscale-oauth-key"  = "${local.ctrl_cluster.name}-tailscale-operator-oauth"
  }

  # ACME DNS-01 reads ctrl-aws-usw2-cloudflare-api-token, covered by base IAM wildcard.
  shared_secret_names = []

  registered_cells = concat(
    # Stage 2: Register adopted Phoenix HyperPod cell once deployed
    # [
    #   {
    #     name           = module.cell_aws_usw2.cluster_name
    #     endpoint       = module.cell_aws_usw2.cluster_endpoint
    #     ca_certificate = module.cell_aws_usw2.cluster_ca_certificate
    #     provider       = "aws"
    #     environment    = "production"
    #     labels = merge(local.cell_aws.labels, {
    #       flavor = "hyperpod-adopted"
    #     })
    #     token          = try(module.argo_cell_rbac_cell_aws_usw2.token, null)
    #     annotations = {
    #       "aws-account-id"       = data.aws_caller_identity.current.account_id
    #       "aws-region"           = var.aws_region
    #       "aws-vpc-id"           = module.cell_aws_usw2.vpc_id
    #       "backups-bucket"       = module.cell_aws_usw2.record.backups_bucket
    #       "control-gateway-ipv4" = local.ctrl_gateway_ipv4
    #       "ecr-registry"         = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
    #       "resource-prefix"      = var.resource_prefix
    #       "s3-endpoint"          = ""
    #       "secret-store"         = "runtime-secrets"
    #       "service-cidr"         = local.cell_aws.network.service_cidr
    #       "tailscale-oauth-key"  = "${module.cell_aws_usw2.cluster_name}-tailscale-operator-oauth"
    #     }
    #   }
    # ],
    []
  )
}

# ==============================================================================
# Stage 2: Adopt Existing Phoenix HyperPod EKS Cluster
# Uses topologies/cell/aws feature toggles to bind to pre-existing VPC/cluster
# without provisioning new VPC or Karpenter controllers.
# ==============================================================================
# module "cell_aws_usw2" {
#   source = "../../topologies/cell/aws"
#
#   cluster_name                  = coalesce(var.cell_aws_cluster_name, "sagemaker-hyperpod-eks-cluster-phx")
#   disabled_components           = ["cluster", "vpc", "karpenter"]
#   vpc_cidr                      = local.cell_aws.vpc_cidr
#   availability_zones            = local.cell_aws.availability_zones
#   tier_subnets                  = local.cell_aws.tier_subnets
#   kubernetes_version            = "1.36"
#   enable_kms_secrets_encryption = true
#   enable_flow_logs              = true
#   enable_control_plane_logging  = true
#   resource_prefix               = var.resource_prefix
#   system_instance_types         = var.system_instance_types
#   domain_name                   = "cell-aws-usw2.${local.public_domain}"
#   parent_zone_id                = module.control_plane.dns_zone_id
#   tailnet_auth_key              = var.tailnet_auth_key
#   fleet_availability            = "resilient"
#   service_ipv4_cidr             = local.cell_aws.network.service_cidr
# }

# module "cell_gcp_euw4" {
#   count  = var.enable_gcp_cell ? 1 : 0
#   source = "../../topologies/cell/gcp"
#
#   cluster_name                  = local.cell_gcp.name
#   resource_prefix               = var.resource_prefix
#   vpc_cidr                      = local.cell_gcp.vpc_cidr
#   availability_zones            = local.cell_gcp.availability_zones
#   tier_subnets                  = local.cell_gcp.tier_subnets
#   kubernetes_version            = "1.36"
#   enable_kms_secrets_encryption = true
#   enable_flow_logs              = true
#   enable_control_plane_logging  = true
#   domain_name                   = "cell-gcp-euw4.${local.public_domain}"
#   tailnet_auth_key              = var.tailnet_auth_key
#   fleet_availability            = "resilient"
#   service_ipv4_cidr             = local.cell_gcp.network.service_cidr
# }
#
# resource "aws_route53_record" "cell_gcp_ns" {
#   count   = var.enable_gcp_cell ? 1 : 0
#   zone_id = module.control_plane.dns_zone_id
#   name    = "cell-gcp-euw4.${local.public_domain}"
#   type    = "NS"
#   ttl     = 300
#   records = module.cell_gcp_euw4[0].name_servers
# }

# Stage 2: Cell RBAC for adopted cell
# module "argo_cell_rbac_cell_aws_usw2" {
#   source = "../../components/argo_cell_rbac"
#
#   providers = {
#     kubernetes = kubernetes.cell_aws
#   }
# }

# module "argo_cell_rbac_cell_gcp_euw4" {
#   count  = var.enable_gcp_cell ? 1 : 0
#   source = "../../components/argo_cell_rbac"
#
#   providers = {
#     kubernetes = kubernetes.cell_gcp
#   }
# }

# Stage 2: Coder cell kubeconfig for adopted cell
# resource "kubernetes_secret_v1" "coder_cell_kubeconfig" {
#   metadata {
#     name      = "coder-cell-kubeconfig"
#     namespace = "kube-system"
#     labels = {
#       "app.kubernetes.io/component"  = "provisioner"
#       "app.kubernetes.io/managed-by" = "opentofu"
#       "app.kubernetes.io/part-of"    = "coder"
#     }
#   }
#
#   data = {
#     kubeconfig = <<-EOT
#       "apiVersion": "v1"
#       "kind": "Config"
#       "clusters":
#       - "cluster":
#           "certificate-authority-data": "${module.cell_aws_usw2.cluster_ca_certificate}"
#           "server": "${module.cell_aws_usw2.cluster_endpoint}"
#         "name": "${module.cell_aws_usw2.cluster_name}"
#       "contexts":
#       - "context":
#           "cluster": "${module.cell_aws_usw2.cluster_name}"
#           "user": "cluster:coder-provisioner:${module.cell_aws_usw2.cluster_name}"
#         "name": "${module.cell_aws_usw2.cluster_name}"
#       "current-context": "${module.cell_aws_usw2.cluster_name}"
#       "users":
#       - "name": "cluster:coder-provisioner:${module.cell_aws_usw2.cluster_name}"
#         "user":
#           "token": "${try(module.argo_cell_rbac_cell_aws_usw2.token, "")}"
#     EOT
#   }
# }

# module "federated_identity_gcp" {
#   count  = var.enable_gcp_cell ? 1 : 0
#   source = "../../components/identity/aws"
#
#   cluster_name            = module.cell_gcp_euw4[0].cluster_name
#   cluster_oidc_issuer_url = module.cell_gcp_euw4[0].oidc_issuer_url
#   trust_mode              = "federated"
#
#   roles = {
#     "examples-ecr-pull" = {
#       namespace       = "team-examples-workloads"
#       service_account = "ecr-pull-token"
#     }
#     "examples-workspace-ecr" = {
#       namespace       = "coder"
#       service_account = "workspace-ecr"
#     }
#   }
# }