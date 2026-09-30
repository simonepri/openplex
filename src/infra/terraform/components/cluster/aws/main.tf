# Provisions AWS EKS clusters, KMS encryption keys, CloudWatch log groups, node security groups, and IAM roles.

module "interface" {
  source = "../_interface"

  cluster_name                  = var.cluster_name
  vpc_id                        = var.vpc_id
  subnet_ids                    = var.subnet_ids
  kubernetes_version            = var.kubernetes_version
  service_ipv4_cidr             = var.service_ipv4_cidr
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_control_plane_logging  = var.enable_control_plane_logging

  realized = {
    cluster_name      = aws_eks_cluster.this.name
    endpoint          = aws_eks_cluster.this.endpoint
    ca_certificate    = aws_eks_cluster.this.certificate_authority[0].data
    oidc_issuer_url   = aws_eks_cluster.this.identity[0].oidc[0].issuer
    oidc_provider_arn = aws_iam_openid_connect_provider.this.arn
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_kms_key" "secrets" {
  count = var.enable_kms_secrets_encryption ? 1 : 0

  description             = "Envelope encryption for ${module.interface.names.cluster} Kubernetes secrets"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_kms_key" "cloudwatch" {
  count = var.enable_control_plane_logging ? 1 : 0

  description             = "Customer-managed key for ${module.interface.names.cluster} CloudWatch log group"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootPermissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "cluster" {
  count = var.enable_control_plane_logging ? 1 : 0

  name              = "/aws/eks/${module.interface.names.cluster}/cluster"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.cloudwatch[0].arn
}

resource "aws_iam_role" "cluster" {
  name = "${module.interface.names.cluster}-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "eks.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role" "nodes" {
  name = "${module.interface.names.cluster}-nodes"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "nodes_worker" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.nodes.name
}

resource "aws_iam_role_policy_attachment" "nodes_cni" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.nodes.name
}

resource "aws_iam_role_policy_attachment" "nodes_registry" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.nodes.name
}

resource "aws_eks_cluster" "this" {
  name     = module.interface.names.cluster
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = length(var.public_access_cidrs) > 0
    public_access_cidrs     = length(var.public_access_cidrs) > 0 ? var.public_access_cidrs : null
  }

  dynamic "kubernetes_network_config" {
    for_each = var.service_ipv4_cidr != null ? [1] : []
    content {
      service_ipv4_cidr = var.service_ipv4_cidr
    }
  }

  dynamic "access_config" {
    for_each = var.enable_access_config ? [1] : []
    content {
      authentication_mode = "API"
    }
  }

  enabled_cluster_log_types = var.enable_control_plane_logging ? ["api", "audit", "authenticator", "controllerManager", "scheduler"] : []

  dynamic "encryption_config" {
    for_each = var.enable_kms_secrets_encryption ? [1] : []
    content {
      provider {
        key_arn = aws_kms_key.secrets[0].arn
      }
      resources = ["secrets"]
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
    aws_kms_key.secrets,
  ]

  lifecycle {
    ignore_changes = [
      access_config[0].bootstrap_cluster_creator_admin_permissions,
      access_config[0].authentication_mode,
    ]
  }
}

resource "aws_eks_addon" "vpc_cni" {
  count = var.enable_addons ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  configuration_values = jsonencode({
    enableNetworkPolicy = var.enable_network_policy ? "true" : "false"
  })

  depends_on = [
    aws_eks_cluster.this,
    aws_iam_role_policy_attachment.nodes_cni,
  ]
}

resource "aws_launch_template" "system" {
  name_prefix = "${aws_eks_cluster.this.name}-system-"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
}

resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.subnet_ids

  launch_template {
    id      = aws_launch_template.system.id
    version = aws_launch_template.system.latest_version
  }

  scaling_config {
    desired_size = 2
    max_size     = 4
    # Karpenter requires pod anti-affinity across hosts and zone spread with
    # DoNotSchedule, so its production replica count needs two nodes present.
    min_size = 2
  }

  instance_types = var.system_instance_types

  depends_on = [
    aws_iam_role_policy_attachment.nodes_worker,
    aws_iam_role_policy_attachment.nodes_cni,
    aws_iam_role_policy_attachment.nodes_registry,
  ]
}

resource "aws_eks_addon" "pod_identity_agent" {
  count = var.enable_addons ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "eks-pod-identity-agent"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_node_group.system,
  ]
}

resource "aws_iam_openid_connect_provider" "this" {
  url            = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "9e99a48a9960b14926cc7f3b02372d874a309192",
    "9687e8340d027dc7eb98b965c7dae9e73f443b7e",
  ]
}

resource "aws_eks_access_entry" "nodes" {
  count = var.enable_access_config ? 1 : 0

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.nodes.arn
  type          = "EC2_LINUX"
}

resource "aws_iam_instance_profile" "karpenter" {
  name = "KarpenterNodeInstanceProfile-${module.interface.names.cluster}"
  role = aws_iam_role.nodes.name
}

resource "aws_iam_role" "ebs_csi" {
  count = var.enable_addons && var.enable_ebs_csi ? 1 : 0

  name = "EBSCSI-${module.interface.names.cluster}"

  assume_role_policy = jsonencode({
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
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  count = var.enable_addons && var.enable_ebs_csi ? 1 : 0

  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
  role       = aws_iam_role.ebs_csi[0].name
}

resource "aws_eks_pod_identity_association" "ebs_csi" {
  count = var.enable_addons && var.enable_ebs_csi ? 1 : 0

  cluster_name    = aws_eks_cluster.this.name
  namespace       = "kube-system"
  service_account = "ebs-csi-controller-sa"
  role_arn        = aws_iam_role.ebs_csi[0].arn
}

resource "aws_eks_addon" "ebs_csi" {
  count = var.enable_addons && var.enable_ebs_csi ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_node_group.system,
    aws_eks_pod_identity_association.ebs_csi,
  ]
}

resource "aws_ec2_tag" "cluster_security_group_karpenter" {
  count = var.enable_access_config ? 1 : 0

  resource_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  key         = "karpenter.sh/discovery"
  value       = module.interface.names.cluster
}
