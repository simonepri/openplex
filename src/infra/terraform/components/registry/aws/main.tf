# Provisions AWS ECR container image repositories, KMS encryption keys, and lifecycle expiration policies.

module "interface" {
  source            = "../_interface"
  installation_name = var.installation_name
  repositories      = var.repositories
  realized = {
    registry_url = length(aws_ecr_repository.this) > 0 ? split("/", aws_ecr_repository.this[var.repositories[0]].repository_url)[0] : null
    repositories = {
      for r in var.repositories : r => aws_ecr_repository.this[r].repository_url
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "registry" {
  description             = "Customer-managed key for ${var.installation_name} ECR repositories"
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
