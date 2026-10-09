# Provisions AWS ECR container image repositories, KMS encryption keys, and lifecycle expiration policies.

module "interface" {
  source       = "../_interface"
  cluster_name = var.cluster_name
  repositories = var.repositories
  realized = {
    registry_url = length(var.repositories) > 0 ? split("/", aws_ecr_repository.this[var.repositories[0]].repository_url)[0] : "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.region}.amazonaws.com"
    repositories = {
      for r in var.repositories : r => aws_ecr_repository.this[r].repository_url
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_kms_key" "registry" {
  description             = "Customer-managed key for ${var.cluster_name} ECR repositories"
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
        Sid    = "AllowECRService"
        Effect = "Allow"
        Principal = {
          Service = "ecr.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "registry" {
  name          = "alias/${var.kms_alias_prefix}${var.cluster_name}-registry"
  target_key_id = aws_kms_key.registry.key_id
}

resource "aws_ecr_repository" "this" {
  for_each             = toset(var.repositories)
  name                 = module.interface.names[each.key]
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.registry.arn
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = toset(var.repositories)
  repository = aws_ecr_repository.this[each.key].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 14 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Expire ephemeral dev images older than 7 days"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["dev-"]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 7
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_ecr_repository_creation_template" "src" {
  # ECR stores template prefixes without a trailing slash; "src/" would replace the template on every plan.
  prefix      = "src"
  description = "Template for auto-created repositories matching Bazel package paths under src/"

  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.registry.arn
  }

  resource_tags = {
    ManagedBy = "opentofu"
  }

  lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 14 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Expire ephemeral dev images older than 7 days"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["dev-"]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 7
        }
        action = {
          type = "expire"
        }
      }
    ]
  })

  applied_for = ["CREATE_ON_PUSH", "PULL_THROUGH_CACHE", "REPLICATION"]
}

