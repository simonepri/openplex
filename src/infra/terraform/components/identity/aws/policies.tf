# Generates IAM policy documents and policy attachments for cluster platform components and workloads.

locals {
  barman_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BarmanS3BucketAccess"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}",
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
          "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}/*",
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

  atlantis_plan_statements = concat(
    [
      {
        Sid    = "AtlantisPlanMetadataRead"
        Effect = "Allow"
        Action = [
          # keep-sorted start
          "athena:GetWorkGroup",
          "athena:ListTagsForResource",
          "athena:ListWorkGroups",
          "cloudtrail:DescribeTrails",
          "cloudtrail:GetEventSelectors",
          "cloudtrail:GetInsightSelectors",
          "cloudtrail:GetTrail",
          "cloudtrail:GetTrailStatus",
          "cloudtrail:ListTags",
          "cur:DescribeReportDefinitions",
          "cur:ListTagsForResource",
          "ec2:Describe*",
          "ec2:GetEbsDefaultKmsKeyId",
          "ec2:GetEbsEncryptionByDefault",
          "ec2:GetLaunchTemplateData",
          "ecr:DescribeRepositories",
          "ecr:DescribeRepositoryCreationTemplates",
          "ecr:GetLifecyclePolicy",
          "ecr:GetRepositoryPolicy",
          "ecr:ListTagsForResource",
          "eks:Describe*",
          "eks:List*",
          "events:Describe*",
          "events:List*",
          "glue:GetCrawler",
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetTags",
          "iam:Get*",
          "iam:List*",
          "kms:Describe*",
          "kms:Get*",
          "kms:List*",
          "logs:DescribeLogGroups",
          "logs:ListTagsForResource",
          "logs:ListTagsLogGroup",
          "route53:Get*",
          "route53:List*",
          "route53resolver:Get*",
          "route53resolver:List*",
          "s3:GetAccelerateConfiguration",
          "s3:GetAccountPublicAccessBlock",
          "s3:GetBucket*",
          "s3:GetEncryptionConfiguration",
          "s3:GetIntelligentTieringConfiguration",
          "s3:GetInventoryConfiguration",
          "s3:GetLifecycleConfiguration",
          "s3:GetReplicationConfiguration",
          "s3:ListAllMyBuckets",
          "s3:ListBucket",
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetResourcePolicy",
          "secretsmanager:ListSecretVersionIds",
          "secretsmanager:ListSecrets",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ListQueueTags",
          "sqs:ListQueues",
          # keep-sorted end
        ]
        Resource = "*"
      },
      {
        Sid    = "AtlantisFleetSecretRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Resource = [
          "arn:aws:secretsmanager:*:*:secret:${var.cluster_name}-*",
          "arn:aws:secretsmanager:*:*:secret:cell-*",
        ]
      },
      {
        Sid    = "AtlantisFleetSecretDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
        ]
        Resource = "*"
        Condition = {
          StringLike = {
            "kms:EncryptionContext:SecretARN" = [
              "arn:aws:secretsmanager:*:*:secret:${var.cluster_name}-*",
              "arn:aws:secretsmanager:*:*:secret:cell-*",
            ]
            "kms:ViaService" = "secretsmanager.*.amazonaws.com"
          }
        }
      },
    ],
    var.opentofu_state_bucket != null && var.opentofu_state_bucket != "" ? [
      {
        Sid    = "AtlantisOpenTofuState"
        Effect = "Allow"
        Action = [
          # keep-sorted start
          "s3:DeleteObject",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:PutObject",
          # keep-sorted end
        ]
        Resource = [
          "arn:aws:s3:::${var.opentofu_state_bucket}",
          "arn:aws:s3:::${var.opentofu_state_bucket}/*",
        ]
      }
    ] : []
  )

  atlantis_plan_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.atlantis_plan_statements
  })

  atlantis_management_statements = [
    {
      Sid    = "AtlantisEC2Management"
      Effect = "Allow"
      Action = [
        # keep-sorted start
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
        "ec2:TerminateInstances",
        # keep-sorted end
      ]
      Resource = "*"
    },
    {
      Sid    = "AtlantisEKSManagement"
      Effect = "Allow"
      Action = [
        # keep-sorted start
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
        "eks:UpdateNodegroupVersion",
        # keep-sorted end
      ]
      Resource = "*"
    },
    {
      Sid    = "AtlantisS3BucketManagement"
      Effect = "Allow"
      Action = [
        # keep-sorted start
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
        "s3:PutObject",
        # keep-sorted end
      ]
      Resource = [
        "arn:aws:s3:::${var.cluster_name}-*",
        "arn:aws:s3:::${var.cluster_name}-*/*",
      ]
    },
    {
      Sid    = "AtlantisRoute53Management"
      Effect = "Allow"
      Action = [
        # keep-sorted start
        "route53:ChangeResourceRecordSets",
        "route53:ChangeTagsForResource",
        "route53:CreateHostedZone",
        "route53:DeleteHostedZone",
        "route53:GetChange",
        "route53:GetHostedZone",
        "route53:ListHostedZones",
        "route53:ListHostedZonesByName",
        "route53:ListResourceRecordSets",
        "route53:ListTagsForResource",
        # keep-sorted end
      ]
      Resource = "*"
    },
    {
      Sid    = "AtlantisSecretsManagerManagement"
      Effect = "Allow"
      Action = [
        # keep-sorted start
        "secretsmanager:CreateSecret",
        "secretsmanager:DeleteSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:TagResource",
        "secretsmanager:UntagResource",
        "secretsmanager:UpdateSecret",
        # keep-sorted end
      ]
      Resource = [
        "arn:aws:secretsmanager:*:*:secret:${var.cluster_name}-*",
        "arn:aws:secretsmanager:*:*:secret:cell-*",
      ]
    },
    {
      Sid    = "AtlantisKMSManagement"
      Effect = "Allow"
      Action = [
        # keep-sorted start
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
        "kms:UpdateAlias",
        # keep-sorted end
      ]
      Resource = "*"
    },
    {
      Sid    = "AtlantisIAMRoleManagement"
      Effect = "Allow"
      Action = [
        # keep-sorted start
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
        "iam:UpdateRole",
        # keep-sorted end
      ]
      Resource = [
        "arn:aws:iam::*:role/${var.cluster_name}-*",
        "arn:aws:iam::*:role/*",
      ]
    },
    {
      Sid    = "AtlantisIAMOIDCManagement"
      Effect = "Allow"
      Action = [
        # keep-sorted start
        "iam:CreateOpenIDConnectProvider",
        "iam:DeleteOpenIDConnectProvider",
        "iam:GetOpenIDConnectProvider",
        "iam:TagOpenIDConnectProvider",
        "iam:UntagOpenIDConnectProvider",
        "iam:UpdateOpenIDConnectProviderThumbprint",
        # keep-sorted end
      ]
      Resource = [
        "arn:aws:iam::*:oidc-provider/*",
      ]
    },
  ]

  atlantis_apply_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      local.atlantis_plan_statements,
      local.atlantis_management_statements,
      [
        {
          Sid    = "AtlantisFleetSecretKMSWrite"
          Effect = "Allow"
          Action = [
            # keep-sorted start
            "kms:Decrypt",
            "kms:Encrypt",
            "kms:GenerateDataKey",
            # keep-sorted end
          ]
          Resource = "*"
          Condition = {
            StringLike = {
              "kms:EncryptionContext:SecretARN" = [
                "arn:aws:secretsmanager:*:*:secret:${var.cluster_name}-*",
                "arn:aws:secretsmanager:*:*:secret:cell-*",
              ]
              "kms:ViaService" = "secretsmanager.*.amazonaws.com"
            }
          }
        },
      ]
    )
  })

  aws_role_policies = {
    atlantis = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "AtlantisAssumePlanAndApply"
          Effect = "Allow"
          Action = [
            # keep-sorted start
            "sts:AssumeRole",
            "sts:TagSession",
            # keep-sorted end
          ]
          Resource = concat(
            [for r in aws_iam_role.atlantis_plan : r.arn],
            [for r in aws_iam_role.atlantis_apply : r.arn],
          )
        },
      ]
    })
    "load-balancer" = jsonencode({
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
    "external-dns" = jsonencode({
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
    "cert-manager" = jsonencode({
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
    "ecr-pull" = jsonencode({
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
    "workspace-ecr" = jsonencode({
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
    trivy = jsonencode({
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
            # keep-sorted start
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
            # keep-sorted end
          ]
          Resource = "arn:aws:ecr:*:${data.aws_caller_identity.current.account_id}:repository/*"
        }
      ]
    })
    opencost = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "OpenCostEC2SpotPriceHistory"
          Effect = "Allow"
          Action = [
            "ec2:DescribeSpotPriceHistory",
          ]
          Resource = "*"
        },
      ]
    })
    "buildbuddy-backups" = local.barman_policy
    "coder-backups"      = local.barman_policy
    "db-backups"         = local.barman_policy
    "dragonfly-backups"  = local.barman_policy
    "signoz-backups"     = local.barman_policy
    barman               = local.barman_policy
    "snapshot-portal" = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "SnapshotPortalListBackups"
          Effect = "Allow"
          Action = ["s3:ListBucket"]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}",
            "arn:aws:s3:::cell-*-backups-${data.aws_caller_identity.current.account_id}",
          ]
        },
        {
          Sid    = "SnapshotPortalReadBackups"
          Effect = "Allow"
          Action = ["s3:GetObject"]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}/*",
            "arn:aws:s3:::cell-*-backups-${data.aws_caller_identity.current.account_id}/*",
          ]
        },
        {
          Sid      = "SnapshotPortalDecryptViaS3"
          Effect   = "Allow"
          Action   = ["kms:Decrypt", "kms:DescribeKey"]
          Resource = "*"
          Condition = {
            StringLike = {
              "kms:ViaService" = "s3.*.amazonaws.com"
            }
          }
        },
      ]
    })
    clickhouse = jsonencode({
      Version = "2012-10-17"
      Statement = concat(
        [
          {
            Sid      = "ClickHouseInventoryList"
            Effect   = "Allow"
            Action   = ["s3:ListBucket"]
            Resource = [var.storage_meta_bucket_arn]
            Condition = {
              StringLike = {
                "s3:prefix" = ["inventory/*"]
              }
            }
          },
          {
            Sid      = "ClickHouseInventoryRead"
            Effect   = "Allow"
            Action   = ["s3:GetObject"]
            Resource = ["${var.storage_meta_bucket_arn}/inventory/*"]
          },
        ],
        flatten([
          for index, report in var.storage_stats_inventory_reports : [
            {
              Sid      = "ClickHouseInventoryReportList${index}"
              Effect   = "Allow"
              Action   = ["s3:ListBucket"]
              Resource = [report.bucket_arn]
              Condition = {
                StringLike = {
                  "s3:prefix" = report.prefixes
                }
              }
            },
            {
              Sid      = "ClickHouseInventoryReportRead${index}"
              Effect   = "Allow"
              Action   = ["s3:GetObject"]
              Resource = [for prefix in report.prefixes : "${report.bucket_arn}/${prefix}"]
            },
          ]
        ]),
        [
          {
            Sid    = "ClickHouseS3BucketAccess"
            Effect = "Allow"
            Action = [
              "s3:ListBucket",
              "s3:ListBucketMultipartUploads",
            ]
            Resource = [
              "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}",
            ]
          },
          {
            Sid    = "ClickHouseS3ObjectAccess"
            Effect = "Allow"
            Action = [
              "s3:AbortMultipartUpload",
              "s3:DeleteObject",
              "s3:GetObject",
              "s3:ListMultipartUploadParts",
              "s3:PutObject",
            ]
            Resource = [
              "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}/*",
            ]
          },
          {
            Sid    = "ClickHouseKMSAccess"
            Effect = "Allow"
            Action = [
              "kms:Decrypt",
              "kms:DescribeKey",
              "kms:Encrypt",
              "kms:GenerateDataKey*",
              "kms:ReEncrypt*",
            ]
            Resource = var.storage_kms_key_arn
            Condition = {
              StringEquals = {
                "kms:ViaService" = "s3.${data.aws_region.current.region}.amazonaws.com"
              }
            }
          },
        ]
      )
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
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}",
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
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}/*",
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
    "workspace-backups" = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "OrgWorkspaceBackupsS3BucketAccess"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
            "s3:ListBucketMultipartUploads",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}",
          ]
        },
        {
          Sid    = "OrgWorkspaceBackupsS3ObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:ListMultipartUploadParts",
            "s3:PutObject",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}/*",
          ]
        },
        {
          Sid    = "OrgWorkspaceBackupsKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:Encrypt",
            "kms:GenerateDataKey*",
            "kms:ReEncrypt*",
          ]
          Resource = var.storage_kms_key_arn
          Condition = {
            StringEquals = {
              "kms:ViaService" = "s3.${data.aws_region.current.region}.amazonaws.com"
            }
          }
        },
      ]
    })
    "storage-stats" = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "StorageStatsInventoryList"
          Effect   = "Allow"
          Action   = "s3:ListBucket"
          Resource = var.storage_meta_bucket_arn
          Condition = {
            StringLike = {
              "s3:prefix" = "inventory/*"
            }
          }
        },
        {
          Sid      = "StorageStatsInventoryRead"
          Effect   = "Allow"
          Action   = "s3:GetObject"
          Resource = "${var.storage_meta_bucket_arn}/inventory/*"
        },
        {
          Sid      = "StorageStatsKMSDecrypt"
          Effect   = "Allow"
          Action   = "kms:Decrypt"
          Resource = var.storage_kms_key_arn
          Condition = {
            StringEquals = {
              "kms:ViaService" = "s3.${data.aws_region.current.region}.amazonaws.com"
            }
          }
        },
      ]
    })
    legacy-research-data = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "LegacyResearchDataBucketAccess"
          Effect = "Allow"
          Action = [
            "s3:GetBucketLocation",
            "s3:ListBucket",
            "s3:ListBucketMultipartUploads",
          ]
          Resource = [
          ]
        },
        {
          Sid    = "LegacyResearchDataObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:ListMultipartUploadParts",
            "s3:PutObject",
          ]
          Resource = [
          ]
        },
        {
          Sid    = "LegacyResearchDataKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:Encrypt",
            "kms:GenerateDataKey*",
            "kms:ReEncrypt*",
          ]
          Resource = [
            "arn:aws:kms:us-west-2:400920695547:key/378523ce-7fc8-475c-a35a-65a5e95671df",
            "arn:aws:kms:us-east-1:421498156696:key/7721ed41-9c4f-47a9-a10f-09a020a3e078",
          ]
          Condition = {
            StringLike = {
              "kms:ViaService" = "s3.*.amazonaws.com"
            }
          }
        },
        {
          Sid      = "LegacyResearchDataLakeAccess"
          Effect   = "Allow"
          Action   = ["sts:AssumeRole", "sts:TagSession"]
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
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}",
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
            "arn:aws:s3:::${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}/*",
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
    "cloud-telemetry" = jsonencode({
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
            "arn:aws:s3:::${var.cluster_name}-logs-${data.aws_caller_identity.current.account_id}",
            "arn:aws:s3:::${var.cluster_name}-logs-${data.aws_caller_identity.current.account_id}/*",
            "arn:aws:s3:::${var.cluster_name}-billing-access-logs-${data.aws_caller_identity.current.account_id}",
            "arn:aws:s3:::${var.cluster_name}-billing-access-logs-${data.aws_caller_identity.current.account_id}/*",
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
    "external-secrets" = jsonencode({
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
            "arn:aws:s3:::${var.cluster_name}-profiles-${data.aws_caller_identity.current.account_id}",
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
            "arn:aws:s3:::${var.cluster_name}-profiles-${data.aws_caller_identity.current.account_id}/*",
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
          Resource = var.storage_kms_key_arn
          Condition = {
            StringEquals = {
              "kms:ViaService" = "s3.${data.aws_region.current.region}.amazonaws.com"
            }
          }
        },
      ]
    })
    kargo = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "KargoECRAuth"
          Effect   = "Allow"
          Action   = ["ecr:GetAuthorizationToken"]
          Resource = "*"
        },
        {
          Sid    = "KargoDiscoverWorkloadImages"
          Effect = "Allow"
          Action = [
            "ecr:BatchGetImage",
            "ecr:DescribeImages",
            "ecr:GetDownloadUrlForLayer",
            "ecr:ListImages",
          ]
          Resource = "arn:aws:ecr:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:repository/src/*"
        }
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

  team_s3_gateway_policies = {
    for k, v in var.roles : k => jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "TeamHomeBucketList"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
            "s3:ListBucketMultipartUploads",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-home-${data.aws_caller_identity.current.account_id}",
          ]
          Condition = {
            StringLike = {
              "s3:prefix" = [
                "home/${trimprefix(k, "s3-gateway-")}/*",
                "home/${trimprefix(k, "s3-gateway-")}",
                "home/${trimprefix(k, "s3-gateway-")}/",
                # rclone checks the parent prefix before reading or writing the team prefix.
                "home/",
                "home",
              ]
            }
          }
        },
        {
          Sid    = "TeamHomeObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:ListMultipartUploadParts",
            "s3:PutObject",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-home-${data.aws_caller_identity.current.account_id}/home/${trimprefix(k, "s3-gateway-")}/*",
          ]
        },
        {
          Sid    = "TeamScratchBucketList"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
            "s3:ListBucketMultipartUploads",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-scratch-${data.aws_caller_identity.current.account_id}",
          ]
          Condition = {
            StringLike = {
              "s3:prefix" = [
                "scratch/${trimprefix(k, "s3-gateway-")}/*",
                "scratch/${trimprefix(k, "s3-gateway-")}",
                "scratch/${trimprefix(k, "s3-gateway-")}/",
                # rclone checks the parent prefix before reading or writing the team prefix.
                "scratch/",
                "scratch",
              ]
            }
          }
        },
        {
          Sid    = "TeamScratchObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:AbortMultipartUpload",
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:ListMultipartUploadParts",
            "s3:PutObject",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-scratch-${data.aws_caller_identity.current.account_id}/scratch/${trimprefix(k, "s3-gateway-")}/*",
          ]
        },
        {
          Sid    = "TeamMetaBucketList"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            var.storage_meta_bucket_arn,
          ]
          Condition = {
            StringLike = {
              "s3:prefix" = [
                "meta/*",
                "meta",
                "meta/",
              ]
            }
          }
        },
        {
          Sid    = "TeamMetaObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:GetObject",
          ]
          Resource = [
            "${var.storage_meta_bucket_arn}/meta/*",
          ]
        },
        {
          Sid    = "TeamKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey",
            "kms:Encrypt",
            "kms:GenerateDataKey*",
            "kms:ReEncrypt*",
          ]
          Resource = var.storage_kms_key_arn
          Condition = {
            StringEquals = {
              "kms:ViaService" = "s3.${data.aws_region.current.region}.amazonaws.com"
            }
          }
        },
      ]
    })
    if startswith(k, "s3-gateway-") && !endswith(k, "-reader")
  }

  team_s3_gateway_reader_policies = {
    for k, v in var.roles : k => jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid    = "TeamHomeBucketList"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-home-${data.aws_caller_identity.current.account_id}",
          ]
          Condition = {
            StringLike = {
              "s3:prefix" = [
                "home/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}/*",
                "home/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}",
                "home/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}/",
                # rclone checks the parent prefix before reading or writing the team prefix.
                "home/",
                "home",
              ]
            }
          }
        },
        {
          Sid    = "TeamHomeObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:GetObject",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-home-${data.aws_caller_identity.current.account_id}/home/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}/*",
          ]
        },
        {
          Sid    = "TeamScratchBucketList"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-scratch-${data.aws_caller_identity.current.account_id}",
          ]
          Condition = {
            StringLike = {
              "s3:prefix" = [
                "scratch/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}/*",
                "scratch/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}",
                "scratch/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}/",
                # rclone checks the parent prefix before reading or writing the team prefix.
                "scratch/",
                "scratch",
              ]
            }
          }
        },
        {
          Sid    = "TeamScratchObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:GetObject",
          ]
          Resource = [
            "arn:aws:s3:::${var.cluster_name}-scratch-${data.aws_caller_identity.current.account_id}/scratch/${trimsuffix(trimprefix(k, "s3-gateway-"), "-reader")}/*",
          ]
        },
        {
          Sid    = "TeamMetaBucketList"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
          ]
          Resource = [
            var.storage_meta_bucket_arn,
          ]
          Condition = {
            StringLike = {
              "s3:prefix" = [
                "meta/*",
                "meta",
                "meta/",
              ]
            }
          }
        },
        {
          Sid    = "TeamMetaObjectAccess"
          Effect = "Allow"
          Action = [
            "s3:GetObject",
          ]
          Resource = [
            "${var.storage_meta_bucket_arn}/meta/*",
          ]
        },
        {
          Sid    = "TeamKMSAccess"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
          ]
          Resource = var.storage_kms_key_arn
          Condition = {
            StringEquals = {
              "kms:ViaService" = "s3.${data.aws_region.current.region}.amazonaws.com"
            }
          }
        },
      ]
    })
    if startswith(k, "s3-gateway-") && endswith(k, "-reader")
  }

  active_policies = {
    for k, v in var.roles : k => (
      contains(keys(local.aws_role_policies), k) ? local.aws_role_policies[k] :
      contains(keys(local.team_s3_gateway_policies), k) ? local.team_s3_gateway_policies[k] :
      contains(keys(local.team_s3_gateway_reader_policies), k) ? local.team_s3_gateway_reader_policies[k] :
      endswith(k, "-ecr-pull") ? local.aws_role_policies["ecr-pull"] :
      local.barman_policy
    )
    if contains(keys(local.aws_role_policies), k) ||
    contains(keys(local.team_s3_gateway_policies), k) ||
    contains(keys(local.team_s3_gateway_reader_policies), k) ||
    endswith(k, "-ecr-pull") ||
    endswith(k, "-backups")
  }
}
