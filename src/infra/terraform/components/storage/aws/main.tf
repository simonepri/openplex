# Provisions AWS S3 buckets, KMS customer-managed keys, versioning, and public access blocks.

module "interface" {
  source = "../_interface"

  installation_name = var.installation_name
  cell_name         = var.cell_name
  storage_tiers     = var.storage_tiers
  realized = {
    buckets = {
      for k, b in aws_s3_bucket.this : k => {
        name        = b.id
        arn         = b.arn
        domain_name = b.bucket_domain_name
      }
    }
    kms_key_arn = aws_kms_key.storage.arn
  }
}

resource "aws_s3_bucket" "this" {
  for_each = toset(var.storage_tiers)

  bucket = module.interface.names[each.key]
}

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "storage" {
  description             = "KMS key for ${var.cell_name} S3 storage tiers"
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
        Sid    = "AllowS3LogDelivery"
        Effect = "Allow"
        Principal = {
          Service = "logging.s3.amazonaws.com"
        }
        Action = [
          "kms:GenerateDataKey*",
          "kms:Decrypt"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = toset(var.storage_tiers)

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.storage.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "this" {
  for_each = contains(var.storage_tiers, "logs") ? toset(var.storage_tiers) : toset([])

  bucket        = aws_s3_bucket.this[each.key].id
  target_bucket = aws_s3_bucket.this["logs"].id
  target_prefix = "s3-access-logs/${each.key}/"
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = toset(var.storage_tiers)

  bucket = aws_s3_bucket.this[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = toset(var.storage_tiers)

  bucket = aws_s3_bucket.this[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "enforce_tls" {
  for_each = toset(var.storage_tiers)

  bucket = aws_s3_bucket.this[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid       = "EnforceTLSRequestsOnly"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:*"
          Resource = [
            aws_s3_bucket.this[each.key].arn,
            "${aws_s3_bucket.this[each.key].arn}/*",
          ]
          Condition = {
            Bool = {
              "aws:SecureTransport" = "false"
            }
          }
        },
        {
          Sid       = "DenyUnencryptedObjectUploads"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:PutObject"
          Resource  = "${aws_s3_bucket.this[each.key].arn}/*"
          Condition = {
            StringNotEquals = {
              "s3:x-amz-server-side-encryption" = "aws:kms"
            }
            Null = {
              "s3:x-amz-server-side-encryption" = "false"
            }
          }
        },
      ],
      each.key == "logs" ? [
        {
          Sid    = "AllowS3ServerAccessLogsDelivery"
          Effect = "Allow"
          Principal = {
            Service = "logging.s3.amazonaws.com"
          }
          Action   = "s3:PutObject"
          Resource = "${aws_s3_bucket.this[each.key].arn}/s3-access-logs/*"
          Condition = {
            ArnLike = {
              "aws:SourceArn" = "arn:aws:s3:::*"
            }
            StringEquals = {
              "aws:SourceAccount" = data.aws_caller_identity.current.account_id
            }
          }
        }
      ] : [],
      each.key == "meta" ? [
        {
          Sid    = "AllowS3InventoryDelivery"
          Effect = "Allow"
          Principal = {
            Service = "s3.amazonaws.com"
          }
          Action   = "s3:PutObject"
          Resource = "${aws_s3_bucket.this[each.key].arn}/inventory/*"
          Condition = {
            ArnLike = {
              "aws:SourceArn" = "arn:aws:s3:::*"
            }
            StringEquals = {
              "aws:SourceAccount" = data.aws_caller_identity.current.account_id
            }
          }
        }
      ] : []
    )
  })
}

resource "aws_s3_bucket_inventory" "this" {
  for_each = toset([for tier in var.storage_tiers : tier if contains(["home", "scratch", "meta", "backups", "archive"], tier)])

  bucket = aws_s3_bucket.this[each.key].id
  name   = "stats"

  included_object_versions = "Current"

  schedule {
    frequency = "Daily"
  }

  destination {
    bucket {
      bucket_arn = aws_s3_bucket.this["meta"].arn
      format     = "Parquet"
      prefix     = "inventory"
    }
  }

  optional_fields = [
    "Size",
    "LastModifiedDate",
  ]
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = toset([for tier in var.storage_tiers : tier if contains(["scratch", "meta"], tier)])

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    id     = "expire-scratch-meta"
    status = "Enabled"

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_lifecycle_configuration" "profiles" {
  for_each = toset([for tier in var.storage_tiers : tier if tier == "profiles"])

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    id     = "expire-profiles"
    status = "Enabled"

    expiration {
      days = var.profiles_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.profiles_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "cleanup-expired-delete-markers"
    status = "Enabled"

    expiration {
      expired_object_delete_marker = true
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}
