# Provisions AWS IAM roles, OIDC federated trust policies, and EKS Pod Identity associations.

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "federated" {
  count = var.trust_mode == "federated" && var.cluster_oidc_arn == "" && var.cluster_oidc_issuer_url != "" ? 1 : 0

  url             = var.cluster_oidc_issuer_url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.oidc_thumbprints
}

locals {
  oidc_provider_arn = coalesce(var.cluster_oidc_arn, try(aws_iam_openid_connect_provider.federated[0].arn, ""))
  clean_oidc_issuer = replace(var.cluster_oidc_issuer_url, "https://", "")
}

module "interface" {
  source = "../_interface"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = var.cluster_oidc_issuer_url
  cluster_oidc_arn        = local.oidc_provider_arn
  project_id              = var.project_id
  roles                   = var.roles
  trust_mode              = var.trust_mode
  realized = {
    role_arns = {
      for k, v in aws_iam_role.this : k => v.arn
    }
  }
}

resource "aws_iam_role" "this" {
  for_each = var.roles

  name = module.interface.names[each.key]

  assume_role_policy = var.trust_mode == "federated" ? jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.clean_oidc_issuer}:aud" = "sts.amazonaws.com"
            "${local.clean_oidc_issuer}:sub" = "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
          }
        }
      }
    ]
    }) : jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "pods.eks.amazonaws.com"
        }
        Action = [
          "sts:AssumeRole",
          "sts:TagSession",
        ]
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })
}

resource "aws_eks_pod_identity_association" "this" {
  for_each = var.trust_mode == "pod_identity" ? var.roles : {}

  cluster_name    = var.cluster_name
  namespace       = each.value.namespace
  service_account = each.value.service_account
  role_arn        = aws_iam_role.this[each.key].arn

  depends_on = [aws_iam_role.this]
}


resource "aws_iam_role_policy" "scoped" {
  for_each = local.active_policies

  name   = "${aws_iam_role.this[each.key].name}-policy"
  role   = aws_iam_role.this[each.key].id
  policy = each.value
}
