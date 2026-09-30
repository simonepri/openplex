# Generates IAM policy documents and policy attachments for cluster platform components and workloads.

locals {
  aws_role_policies = {
    atlantis = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "AtlantisEC2Management"
          Effect = "Allow"
          Action = [
            "ec2:AllocateAddress",
            "ec2:AssociateRouteTable",
            "ec2:AttachInternetGateway",
            "ec2:AuthorizeSecurityGroupEgress",
            "ec2:AuthorizeSecurityGroupIngress",
            "ec2:CreateFlowLogs",
            "ec2:CreateInternetGateway",
            "ec2:CreateNatGateway",
            "ec2:CreateRoute",
            "ec2:CreateRouteTable",
            "ec2:CreateSecurityGroup",
            "ec2:CreateSubnet",
            "ec2:CreateTags",
            "ec2:CreateVpc",
            "ec2:DeleteFlowLogs",
            "ec2:DeleteInternetGateway",
            "ec2:DeleteNatGateway",
            "ec2:DeleteRoute",
            "ec2:DeleteRouteTable",
            "ec2:DeleteSecurityGroup",
            "ec2:DeleteSubnet",
            "ec2:DeleteTags",
            "ec2:DeleteVpc",
            "ec2:Describe*",
            "ec2:DetachInternetGateway",
            "ec2:DisassociateRouteTable",
            "ec2:ModifySubnetAttribute",
            "ec2:ModifyVpcAttribute",
            "ec2:ReleaseAddress",
            "ec2:RevokeSecurityGroupEgress",
            "ec2:RevokeSecurityGroupIngress",
            "ec2:RunInstances",
            "ec2:TerminateInstances"
          ]
          Resource = "*"
        },
        {
          Sid    = "AtlantisEKSManagement"
          Effect = "Allow"
          Action = [
            "eks:AssociateAccessPolicy",
            "eks:CreateAccessEntry",
            "eks:CreateCluster",
            "eks:CreateNodegroup",
            "eks:DeleteAccessEntry",
            "eks:DeleteCluster",
            "eks:DeleteNodegroup",
            "eks:DescribeAccessEntry",
            "eks:DescribeCluster",
            "eks:DescribeNodegroup",
            "eks:DisassociateAccessPolicy",
            "eks:ListAccessEntries",
            "eks:ListAssociatedAccessPolicies",
            "eks:ListClusters",
            "eks:ListNodegroups",
            "eks:ListTagsForResource",
            "eks:TagResource",
            "eks:UntagResource",
            "eks:UpdateClusterConfig",
            "eks:UpdateClusterVersion",
            "eks:UpdateNodegroupConfig",
            "eks:UpdateNodegroupVersion"
          ]
          Resource = "*"
        },
        {
          Sid    = "AtlantisS3BucketManagement"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:CreateBucket",
            "s3:DeleteBucket",
            "s3:DeleteBucketPolicy",
            "s3:DeleteObject",
            "s3:GetBucket*",
            "s3:GetEncryptionConfiguration",
            "s3:GetLifecycleConfiguration",
            "s3:GetObject",
            "s3:ListBucket",
            "s3:ListMultipartUploadParts",
            "s3:PutBucket*",
            "s3:PutEncryptionConfiguration",
            "s3:PutLifecycleConfiguration",
            "s3:PutObject"
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-*",
            "arn:aws:s3:::${var.cluster_name}-*/*",
            "arn:aws:s3:::*-tf-state*",
            "arn:aws:s3:::*-tf-state*/*"
          ]
        },
        {
          Sid    = "AtlantisS3ListAll"
          Effect = "Allow"
          Action = [
            "s3:ListAllMyBuckets"
          ]
          Resource = "*"
        },
        {
          Sid    = "AtlantisRoute53Management"
          Effect = "Allow"
          Action = [
            "route53:ChangeResourceRecordSets",
            "route53:ChangeTagsForResource",
            "route53:CreateHostedZone",
            "route53:DeleteHostedZone",
            "route53:GetChange",
            "route53:GetHostedZone",
            "route53:ListHostedZones",
            "route53:ListHostedZonesByName",
            "route53:ListResourceRecordSets",
            "route53:ListTagsForResource"
          ]
          Resource = "*"
        },
        {
          Sid    = "AtlantisSecretsManagerManagement"
          Effect = "Allow"
          Action = [
            "secretsmanager:CreateSecret",
            "secretsmanager:DeleteSecret",
            "secretsmanager:DescribeSecret",
            "secretsmanager:GetSecretValue",
            "secretsmanager:PutSecretValue",
            "secretsmanager:TagResource",
            "secretsmanager:UntagResource",
            "secretsmanager:UpdateSecret"
          ]
          Resource = [
            "arn:aws:secretsmanager:*:*:secret:${var.cluster_name}-*",
            "arn:aws:secretsmanager:*:*:secret:*"
          ]
        },
        {
          Sid    = "AtlantisSecretsManagerList"
          Effect = "Allow"
          Action = [
            "secretsmanager:ListSecrets"
          ]
          Resource = "*"
        },
        {
          Sid    = "AtlantisKMSManagement"
          Effect = "Allow"
          Action = [
            "kms:CancelKeyDeletion",
            "kms:CreateAlias",
            "kms:CreateKey",
            "kms:DeleteAlias",
            "kms:DescribeKey",
            "kms:DisableKey",
            "kms:EnableKey",
            "kms:GetKeyPolicy",
            "kms:ListAliases",
            "kms:ListKeys",
            "kms:ListResourceTags",
            "kms:PutKeyPolicy",
            "kms:ScheduleKeyDeletion",
            "kms:TagResource",
            "kms:UntagResource",
            "kms:UpdateAlias"
          ]
          Resource = "*"
        },
        {
          Sid    = "AtlantisIAMRoleManagement"
          Effect = "Allow"
          Action = [
            "iam:AttachRolePolicy",
            "iam:CreateRole",
            "iam:DeleteRole",
            "iam:DeleteRolePolicy",
            "iam:DetachRolePolicy",
            "iam:GetRole",
            "iam:GetRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:ListInstanceProfilesForRole",
            "iam:ListRolePolicies",
            "iam:PassRole",
            "iam:PutRolePolicy",
            "iam:TagRole",
            "iam:UntagRole",
            "iam:UpdateRole"
          ]
          Resource = [
            "arn:aws:iam::*:role/${var.cluster_name}-*",
            "arn:aws:iam::*:role/*"
          ]
        },
        {
          Sid    = "AtlantisIAMOIDCManagement"
          Effect = "Allow"
          Action = [
            "iam:CreateOpenIDConnectProvider",
            "iam:DeleteOpenIDConnectProvider",
            "iam:GetOpenIDConnectProvider",
            "iam:TagOpenIDConnectProvider",
            "iam:UntagOpenIDConnectProvider",
            "iam:UpdateOpenIDConnectProviderThumbprint"
          ]
          Resource = [
            "arn:aws:iam::*:oidc-provider/*"
          ]
        },
        {
          Sid    = "AtlantisIAMListAndRead"
          Effect = "Allow"
          Action = [
            "iam:GetPolicy",
            "iam:GetPolicyVersion",
            "iam:ListOpenIDConnectProviders",
            "iam:ListPolicies",
            "iam:ListRoles"
          ]
          Resource = "*"
        }
      ]
    })
    aws_load_balancer_controller = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "AWSLoadBalancerControllerELB"
          Effect = "Allow"
          Action = [
            "elasticloadbalancing:Describe*",
            "elasticloadbalancing:AddTags",
            "elasticloadbalancing:RemoveTags",
            "elasticloadbalancing:CreateListener",
            "elasticloadbalancing:DeleteListener",
            "elasticloadbalancing:CreateRule",
            "elasticloadbalancing:DeleteRule",
            "elasticloadbalancing:ModifyListener",
            "elasticloadbalancing:ModifyRule",
            "elasticloadbalancing:CreateLoadBalancer",
            "elasticloadbalancing:DeleteLoadBalancer",
            "elasticloadbalancing:ModifyLoadBalancerAttributes",
            "elasticloadbalancing:SetIpAddressType",
            "elasticloadbalancing:SetSecurityGroups",
            "elasticloadbalancing:SetSubnets",
            "elasticloadbalancing:CreateTargetGroup",
            "elasticloadbalancing:DeleteTargetGroup",
            "elasticloadbalancing:ModifyTargetGroup",
            "elasticloadbalancing:ModifyTargetGroupAttributes",
            "elasticloadbalancing:RegisterTargets",
            "elasticloadbalancing:DeregisterTargets"
          ]
          Resource = "*"
        },
        {
          Sid    = "AWSLoadBalancerControllerEC2"
          Effect = "Allow"
          Action = [
            "ec2:DescribeAvailabilityZones",
            "ec2:DescribeAccountAttributes",
            "ec2:DescribeAddresses",
            "ec2:DescribeCoipPools",
            "ec2:DescribeInstances",
            "ec2:DescribeInternetGateways",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSubnets",
            "ec2:DescribeTags",
            "ec2:DescribeVpcs",
            "ec2:DescribeVpcAttribute",
            "ec2:GetSecurityGroupsForVpc",
            "ec2:AuthorizeSecurityGroupIngress",
            "ec2:RevokeSecurityGroupIngress",
            "ec2:CreateSecurityGroup",
            "ec2:CreateTags",
            "ec2:DeleteTags",
            "ec2:DeleteSecurityGroup"
          ]
          Resource = "*"
        },
        {
          Sid    = "AWSLoadBalancerControllerIAM"
          Effect = "Allow"
          Action = [
            "iam:CreateServiceLinkedRole",
            "iam:GetServerCertificate",
            "iam:ListServerCertificates"
          ]
          Resource = "*"
        },
        {
          Sid    = "AWSLoadBalancerControllerShieldAndWaf"
          Effect = "Allow"
          Action = [
            "cognito-idp:DescribeUserPoolClient",
            "acm:ListCertificates",
            "acm:DescribeCertificate",
            "waf-regional:*",
            "wafv2:*",
            "shield:*"
          ]
          Resource = "*"
        }
      ]
    })
    karpenter = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "KarpenterControllerEC2"
          Effect = "Allow"
          Action = [
            "ec2:CreateFleet",
            "ec2:CreateLaunchTemplate",
            "ec2:CreateTags",
            "ec2:DescribeAvailabilityZones",
            "ec2:DescribeImages",
            "ec2:DescribeInstances",
            "ec2:DescribeInstanceTypeOfferings",
            "ec2:DescribeInstanceTypes",
            "ec2:DescribeLaunchTemplates",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSpotPriceHistory",
            "ec2:DescribeSubnets",
            "ec2:DeleteLaunchTemplate",
            "ec2:RunInstances",
            "ec2:TerminateInstances",
            "eks:DescribeCluster",
            "pricing:GetProducts",
            "ssm:GetParameter",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
            "sqs:ReceiveMessage"
          ]
          Resource = "*"
        },
        {
          Sid    = "KarpenterPassRole"
          Effect = "Allow"
          Action = ["iam:PassRole"]
          Resource = [
            "arn:aws:iam::*:role/*${var.cluster_name}*node*",
            "arn:aws:iam::*:role/*${var.cluster_name}*nodes*"
          ]
          Condition = {
            StringEquals = {
              "iam:PassedToService" = "ec2.amazonaws.com"
            }
          }
        }
      ]
    })
    external_dns = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "ExternalDNSRoute53Changes"
          Effect = "Allow"
          Action = [
            "route53:ChangeResourceRecordSets"
          ]
          Resource = ["arn:aws:route53:::hostedzone/*"]
        },
        {
          Sid    = "ExternalDNSRoute53List"
          Effect = "Allow"
          Action = [
            "route53:ListHostedZones",
            "route53:ListResourceRecordSets",
            "route53:ListTagsForResources"
          ]
          Resource = ["*"]
        }
      ]
    })
    cert_manager = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "CertManagerRoute53GetChange"
          Effect = "Allow"
          Action = [
            "route53:GetChange"
          ]
          Resource = ["arn:aws:route53:::change/*"]
        },
        {
          Sid    = "CertManagerRoute53Changes"
          Effect = "Allow"
          Action = [
            "route53:ChangeResourceRecordSets",
            "route53:ListResourceRecordSets"
          ]
          Resource = ["arn:aws:route53:::hostedzone/*"]
        },
        {
          Sid    = "CertManagerRoute53ListZones"
          Effect = "Allow"
          Action = [
            "route53:ListHostedZonesByName"
          ]
          Resource = ["*"]
        }
      ]
    })
    coder_reconciler_ecr_pull = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "ECRAuth"
          Effect   = "Allow"
          Action   = ["ecr:GetAuthorizationToken"]
          Resource = "*"
        },
        {
          Sid    = "ECRWorkspacePull"
          Effect = "Allow"
          Action = [
            "ecr:BatchCheckLayerAvailability",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
          ]
          Resource = [
            "arn:aws:ecr:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:repository/src/infra/definitions/workspaces/templates/*",
          ]
        }
      ]
    })
    ecr_pull = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "ECRAuth"
          Effect   = "Allow"
          Action   = ["ecr:GetAuthorizationToken"]
          Resource = "*"
        },
        {
          Sid    = "ECRBatchPull"
          Effect = "Allow"
          Action = [
            "ecr:BatchCheckLayerAvailability",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
          ]
          Resource = "*"
        }
      ]
    })
    workspace_ecr = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "ECRAuth"
          Effect   = "Allow"
          Action   = ["ecr:GetAuthorizationToken"]
          Resource = "*"
        },
        {
          Sid    = "ECRPushPull"
          Effect = "Allow"
          Action = [
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:CompleteLayerUpload",
            "ecr:DescribeRepositories",
            "ecr:GetDownloadUrlForLayer",
            "ecr:InitiateLayerUpload",
            "ecr:PutImage",
            "ecr:UploadLayerPart",
          ]
          Resource = "*"
        }
      ]
    })
    barman = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "BarmanS3BucketAccess"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-backups",
            "arn:aws:s3:::${var.cluster_name}-backups",
          ]
        },
        {
          Sid    = "BarmanS3ObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:PutObject",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-backups/*",
            "arn:aws:s3:::${var.cluster_name}-backups/*",
          ]
        },
        {
          Sid    = "BarmanKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:Encrypt",
            "kms:GenerateDataKey*",
            "kms:ReEncrypt*",
          ]
          Resource = "*"
        },
      ]
    })
    kopia = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "KopiaS3BucketAccess"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-backups",
            "arn:aws:s3:::${var.cluster_name}-backups",
          ]
        },
        {
          Sid    = "KopiaS3ObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:PutObject",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-backups/*",
            "arn:aws:s3:::${var.cluster_name}-backups/*",
          ]
        },
        {
          Sid    = "KopiaKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:Encrypt",
            "kms:GenerateDataKey*",
            "kms:ReEncrypt*",
          ]
          Resource = "*"
        },
      ]
    })
    velero = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "VeleroS3BucketAccess"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-backups",
            "arn:aws:s3:::${var.cluster_name}-backups",
          ]
        },
        {
          Sid    = "VeleroS3ObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:PutObject",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-backups/*",
            "arn:aws:s3:::${var.cluster_name}-backups/*",
          ]
        },
        {
          Sid    = "VeleroKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:Encrypt",
            "kms:GenerateDataKey*",
            "kms:ReEncrypt*",
          ]
          Resource = "*"
        },
      ]
    })
    cloud_telemetry = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "S3AccessLogsRead"
          Effect = "Allow"
          Action = [
            "s3:GetObject",
            "s3:ListBucket",
          ]
          Resource = [
            "arn:aws:s3:::*-logs",
            "arn:aws:s3:::*-logs/*",
            "arn:aws:s3:::*-billing-access-logs",
            "arn:aws:s3:::*-billing-access-logs/*",
          ]
        },
        {
          Sid    = "CloudWatchLogsRead"
          Effect = "Allow"
          Action = [
            "logs:DescribeLogGroups",
            "logs:DescribeLogStreams",
            "logs:GetLogEvents",
            "logs:FilterLogEvents",
          ]
          Resource = "*"
        },
        {
          Sid    = "KMSDecryptTelemetry"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
          ]
          Resource = "*"
        },
      ]
    })
    external_secrets = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "ExternalSecretsReadClusterSecrets"
          Effect = "Allow"
          Action = [
            "secretsmanager:DescribeSecret",
            "secretsmanager:GetSecretValue",
          ]
          # Secrets Manager appends a six-character suffix to every secret ARN.
          Resource = concat(
            [
              "arn:aws:secretsmanager:*:*:secret:${var.cluster_name}-*",
              "arn:aws:secretsmanager:*:*:secret:*-${var.cluster_name}-??????",
            ],
            [for name in var.shared_secret_names : "arn:aws:secretsmanager:*:*:secret:${name}-??????"],
          )
        },
        {
          # Secrets encrypted with a customer-managed key; decryption is only allowed through Secrets Manager,
          # so the role can decrypt no more than the secrets granted above.
          Sid      = "ExternalSecretsDecryptViaSecretsManager"
          Effect   = "Allow"
          Action   = ["kms:Decrypt"]
          Resource = "*"
          Condition = {
            StringLike = {
              "kms:ViaService" = "secretsmanager.*.amazonaws.com"
            }
          }
        },
      ]
    })
    parca = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "ParcaS3BucketAccess"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-profiles",
            "arn:aws:s3:::${var.cluster_name}-profiles",
          ]
        },
        {
          Sid    = "ParcaS3ObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:PutObject",
          ]
          Resource = [
            "arn:aws:s3:::*-${var.cluster_name}-profiles/*",
            "arn:aws:s3:::${var.cluster_name}-profiles/*",
          ]
        },
        {
          Sid    = "ParcaKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:Encrypt",
            "kms:GenerateDataKey*",
            "kms:ReEncrypt*",
          ]
          Resource = var.storage_kms_key_arn != "" ? var.storage_kms_key_arn : "*"
        },
      ]
    })
    prowler = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "ProwlerSecurityAuditRead"
          Effect = "Allow"
          Action = [
            "account:Get*",
            "account:List*",
            "cloudtrail:Describe*",
            "cloudtrail:Get*",
            "cloudtrail:List*",
            "cloudwatch:Describe*",
            "cloudwatch:Get*",
            "cloudwatch:List*",
            "config:BatchGet*",
            "config:DeliverConfigSnapshot",
            "config:Describe*",
            "config:Get*",
            "config:List*",
            "ec2:Describe*",
            "ec2:Get*",
            "eks:AccessEntry*",
            "eks:Describe*",
            "eks:List*",
            "elasticloadbalancing:Describe*",
            "guardduty:Get*",
            "guardduty:List*",
            "iam:Generate*",
            "iam:Get*",
            "iam:List*",
            "kms:Describe*",
            "kms:Get*",
            "kms:GetKeyPolicy",
            "kms:GetKeyRotationStatus",
            "kms:List*",
            "logs:Describe*",
            "logs:FilterLogEvents",
            "logs:Get*",
            "logs:List*",
            "s3:GetAccountPublicAccessBlock",
            "s3:GetBucket*",
            "s3:GetEncryptionConfiguration",
            "s3:GetLifecycleConfiguration",
            "s3:GetReplicationConfiguration",
            "s3:ListAllMyBuckets",
            "s3:ListBucket*",
            "securityhub:BatchGet*",
            "securityhub:Describe*",
            "securityhub:Get*",
            "securityhub:List*",
            "sns:Get*",
            "sns:List*",
          ]
          Resource = "*"
        },
      ]
    })
  }

  active_policies = {
    for k, v in var.roles : k => (
      contains(keys(local.aws_role_policies), k) ? local.aws_role_policies[k] :
      (endswith(k, "barman") ? local.aws_role_policies.barman :
        endswith(k, "reconciler_ecr_pull") || endswith(k, "reconciler-ecr-pull") ? local.aws_role_policies.coder_reconciler_ecr_pull :
        endswith(k, "ecr_pull") || endswith(k, "ecr-pull") ? local.aws_role_policies.ecr_pull :
      endswith(k, "workspace_ecr") || endswith(k, "workspace-ecr") ? local.aws_role_policies.workspace_ecr : null)
    )
    if contains(keys(local.aws_role_policies), k) || endswith(k, "barman") || endswith(k, "reconciler_ecr_pull") || endswith(k, "reconciler-ecr-pull") || endswith(k, "ecr_pull") || endswith(k, "ecr-pull") || endswith(k, "workspace_ecr") || endswith(k, "workspace-ecr")
  }
}

