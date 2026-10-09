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

resource "aws_kms_alias" "secrets" {
  count = var.enable_kms_secrets_encryption ? 1 : 0

  name          = "alias/${var.kms_alias_prefix}${module.interface.names.cluster}-secrets"
  target_key_id = aws_kms_key.secrets[0].key_id
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

resource "aws_kms_alias" "cloudwatch" {
  count = var.enable_control_plane_logging ? 1 : 0

  name          = "alias/${var.kms_alias_prefix}${module.interface.names.cluster}-cloudwatch"
  target_key_id = aws_kms_key.cloudwatch[0].key_id
}

resource "aws_cloudwatch_log_group" "cluster" {
  count = var.enable_control_plane_logging ? 1 : 0

  name              = "/aws/eks/${module.interface.names.cluster}/cluster"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.cloudwatch[0].arn
}

resource "aws_iam_role" "cluster" {
  name                 = "${var.iam_name_prefix}${module.interface.names.cluster}-cluster"
  permissions_boundary = var.iam_permissions_boundary

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
  name                 = "${var.iam_name_prefix}${module.interface.names.cluster}-nodes"
  permissions_boundary = var.iam_permissions_boundary

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

# nosemgrep: repository.security.eks-node-role-disallowed-policy
resource "aws_iam_role_policy" "nodes_cni_cloudwatch" {
  name = "${var.iam_name_prefix}${module.interface.names.cluster}-nodes-cni-cloudwatch"
  role = aws_iam_role.nodes.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/eks/${module.interface.names.cluster}/cluster",
          "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/eks/${module.interface.names.cluster}/cluster:log-stream:aws-network-policy-agent-audit-*",
        ]
      },
    ]
  })
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

  # Pods take addresses from the dedicated pod subnets (custom networking) in /28 blocks (prefix delegation), so node
  # subnets stay small and the per-node pod limit is set by kubelet maxPods instead of the ENI address count.
  configuration_values = local.pod_custom_networking ? jsonencode({
    enableNetworkPolicy = var.enable_network_policy ? "true" : "false"
    nodeAgent = {
      enablePolicyEventLogs = "true"
      enableCloudWatchLogs  = "true"
      logLevel              = "info"
    }
    env = {
      AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG = "true"
      ENI_CONFIG_LABEL_DEF               = "topology.kubernetes.io/zone"
      ENABLE_PREFIX_DELEGATION           = "true"
      WARM_PREFIX_TARGET                 = "1"
    }
    eniConfig = {
      create = true
      region = data.aws_region.current.region
      subnets = {
        for subnet in data.aws_subnet.pod : subnet.availability_zone => {
          id             = subnet.id
          securityGroups = [aws_eks_cluster.this.vpc_config[0].cluster_security_group_id]
        }
      }
    }
    }) : jsonencode({
    enableNetworkPolicy = var.enable_network_policy ? "true" : "false"
    nodeAgent = {
      enablePolicyEventLogs = "true"
      enableCloudWatchLogs  = "true"
      logLevel              = "info"
    }
  })

  # Switching networking modes waits for aws-node to roll across every node; the provider default of 20m aborts mid-roll.
  timeouts {
    update = "45m"
  }

  depends_on = [
    aws_eks_cluster.this,
    aws_iam_role_policy_attachment.nodes_cni,
    aws_iam_role_policy.nodes_cni_cloudwatch,
  ]
}

locals {
  pod_custom_networking = length(var.pod_subnet_ids) > 0
}

data "aws_subnet" "pod" {
  count = length(var.pod_subnet_ids)
  id    = var.pod_subnet_ids[count.index]
}

resource "aws_launch_template" "system" {
  name_prefix = "${aws_eks_cluster.this.name}-system-"

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      encrypted   = true
      volume_type = "gp3"
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Managed node groups merge this nodeadm NodeConfig into their own bootstrap; prefix delegation lifts the ENI limit.
  user_data = local.pod_custom_networking ? base64encode(<<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="//"

    --//
    Content-Type: application/node.eks.aws

    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      kubelet:
        config:
          maxPods: ${var.system_max_pods}

    --//--
    EOT
  ) : null
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
    desired_size = var.system_desired_size
    max_size     = var.system_max_size
    # Karpenter requires pod anti-affinity across hosts and zone spread with
    # DoNotSchedule, so its production replica count needs two nodes present.
    min_size = 2
  }

  instance_types = var.system_instance_types

  dynamic "node_repair_config" {
    for_each = var.node_repair_enabled ? [1] : []
    content {
      enabled = true
    }
  }

  dynamic "taint" {
    for_each = var.system_node_taints
    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = replace(upper(taint.value.effect), "-", "_")
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.nodes_worker,
    aws_iam_role_policy_attachment.nodes_cni,
    aws_iam_role_policy_attachment.nodes_registry,
    aws_iam_role_policy.nodes_cni_cloudwatch,
    # Nodes must boot after the CNI networking mode they will use is configured.
    aws_eks_addon.vpc_cni,
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

resource "aws_eks_addon" "node_monitoring_agent" {
  count = var.enable_addons ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "eks-node-monitoring-agent"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  # Node health conditions must cover every node, including tainted GPU and system nodes.
  configuration_values = jsonencode({
    nodeAgent = {
      tolerations = [{ operator = "Exists" }]
    }
  })

  depends_on = [
    aws_eks_node_group.system,
  ]
}

resource "aws_eks_addon" "metrics_server" {
  count = var.enable_addons ? 1 : 0

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "metrics-server"
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

resource "aws_eks_access_entry" "atlantis_plan" {
  count = var.enable_access_config && length(trimspace(var.atlantis_plan_role_arn)) > 0 ? 1 : 0

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.atlantis_plan_role_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "atlantis_plan" {
  count = var.enable_access_config && length(trimspace(var.atlantis_plan_role_arn)) > 0 ? 1 : 0

  cluster_name  = aws_eks_cluster.this.name
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminViewPolicy"
  principal_arn = var.atlantis_plan_role_arn

  access_scope {
    type = "cluster"
  }

  depends_on = [
    aws_eks_access_entry.atlantis_plan,
  ]
}

resource "aws_eks_access_entry" "atlantis_apply" {
  count = var.enable_access_config && length(trimspace(var.atlantis_apply_role_arn)) > 0 ? 1 : 0

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.atlantis_apply_role_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "atlantis_apply" {
  count = var.enable_access_config && length(trimspace(var.atlantis_apply_role_arn)) > 0 ? 1 : 0

  cluster_name  = aws_eks_cluster.this.name
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  principal_arn = var.atlantis_apply_role_arn

  access_scope {
    type = "cluster"
  }

  depends_on = [
    aws_eks_access_entry.atlantis_apply,
  ]
}

resource "aws_iam_instance_profile" "karpenter" {
  name = "${var.iam_name_prefix}${module.interface.names.cluster}-karpenter-node"
  role = aws_iam_role.nodes.name
}

resource "aws_iam_role" "ebs_csi" {
  count = var.enable_addons && var.enable_ebs_csi ? 1 : 0

  name                 = "${var.iam_name_prefix}${module.interface.names.cluster}-ebs-csi"
  permissions_boundary = var.iam_permissions_boundary

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

  configuration_values = jsonencode({
    controller = merge(
      {
        volumeModificationFeature = {
          enabled = true
        }
      },
      length(var.tags) > 0 ? {
        extraVolumeTags = var.tags
      } : {},
    )
    node = {
      metadataSources = "kubernetes"
    }
  })

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

data "aws_vpc" "this" {
  id = var.vpc_id
}

resource "aws_security_group_rule" "cluster_ingress_vpc" {
  description       = "Allow HTTPS from VPC to EKS private control plane endpoint"
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = [data.aws_vpc.this.cidr_block]
  security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

