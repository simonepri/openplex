# Provisions AWS Cost and Usage Report S3 buckets, KMS encryption keys, and bucket policies.

data "aws_caller_identity" "current" {}

module "interface" {
  source = "../_interface"

  cluster_name            = var.cluster_name
  cluster_oidc_issuer_url = var.cluster_oidc_issuer_url
  cluster_oidc_arn        = var.cluster_oidc_arn
  realized = {
    bucket_name      = aws_s3_bucket.cur.id
    athena_database  = aws_glue_catalog_database.cur.name
    athena_workgroup = aws_athena_workgroup.opencost.name
    role_arn         = aws_iam_role.opencost.arn
  }
}

resource "aws_kms_key" "cur" {
  description             = "KMS key for ${var.cluster_name} billing reports S3 bucket"
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
        Sid    = "AllowBillingReports"
        Effect = "Allow"
        Principal = {
          Service = [
            "bcm-data-exports.amazonaws.com",
            "billingreports.amazonaws.com",
          ]
        }
        Action = [
          "kms:GenerateDataKey*",
          "kms:Decrypt",
        ]
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
          "kms:Decrypt",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_s3_bucket" "cur" {
  bucket = "${var.cluster_name}-billing-reports"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cur" {
  bucket = aws_s3_bucket.cur.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.cur.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket" "cur_access_logs" {
  bucket = "${var.cluster_name}-billing-access-logs"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cur_access_logs" {
  bucket = aws_s3_bucket.cur_access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.cur.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "cur_access_logs" {
  bucket = aws_s3_bucket.cur_access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "cur_access_logs" {
  bucket = aws_s3_bucket.cur_access_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "cur" {
  bucket        = aws_s3_bucket.cur.id
  target_bucket = aws_s3_bucket.cur_access_logs.id
  target_prefix = "s3-access-logs/cur/"
}

resource "aws_s3_bucket_logging" "cur_access_logs" {
  bucket        = aws_s3_bucket.cur_access_logs.id
  target_bucket = aws_s3_bucket.cur_access_logs.id
  target_prefix = "s3-access-logs/access-logs/"
}

resource "aws_s3_bucket_versioning" "cur" {
  bucket = aws_s3_bucket.cur.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "cur" {
  bucket = aws_s3_bucket.cur.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "cur" {
  bucket = aws_s3_bucket.cur.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnforceTLSRequestsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.cur.arn,
          "${aws_s3_bucket.cur.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid    = "AllowBillingReports"
        Effect = "Allow"
        Principal = {
          Service = [
            "bcm-data-exports.amazonaws.com",
            "billingreports.amazonaws.com",
          ]
        }
        Action = [
          "s3:GetBucketAcl",
          "s3:GetBucketPolicy",
        ]
        Resource = aws_s3_bucket.cur.arn
      },
      {
        Sid    = "AllowBillingReportsPutObject"
        Effect = "Allow"
        Principal = {
          Service = [
            "bcm-data-exports.amazonaws.com",
            "billingreports.amazonaws.com",
          ]
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.cur.arn}/*"
      },
    ]
  })
}

resource "aws_cur_report_definition" "cur" {
  depends_on = [aws_s3_bucket_policy.cur]

  report_name                = "${var.cluster_name}-cur"
  time_unit                  = "HOURLY"
  format                     = "Parquet"
  compression                = "Parquet"
  additional_schema_elements = ["RESOURCES"]
  s3_bucket                  = aws_s3_bucket.cur.id
  s3_prefix                  = "cur"
  s3_region                  = var.aws_region
  additional_artifacts       = ["ATHENA"]
  report_versioning          = "OVERWRITE_REPORT"
}

resource "aws_athena_workgroup" "opencost" {
  name = "${var.cluster_name}-opencost"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      # nosemgrep: repository.storage.canonical-virtual-s3-uri - Physical AWS S3 bucket for Athena query results
      output_location = "s3://${aws_s3_bucket.cur.id}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = aws_kms_key.cur.arn
      }
    }
  }
}

resource "aws_glue_catalog_database" "cur" {
  name = replace("${var.cluster_name}_cur", "-", "_")
}

resource "aws_iam_role" "opencost" {
  name = "${var.cluster_name}-opencost"

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

resource "aws_eks_pod_identity_association" "opencost" {
  cluster_name    = var.cluster_name
  namespace       = "opencost"
  service_account = "opencost"
  role_arn        = aws_iam_role.opencost.arn

  depends_on = [aws_iam_role.opencost]
}

resource "aws_iam_role_policy" "opencost" {
  name = "opencost-cur-access"
  role = aws_iam_role.opencost.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AthenaAccess"
        Effect = "Allow"
        Action = [
          "athena:BatchGetQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults",
          "athena:GetWorkGroup",
          "athena:StartQueryExecution",
          "athena:StopQueryExecution",
        ]
        Resource = aws_athena_workgroup.opencost.arn
      },
      {
        Sid    = "GlueCatalogAccess"
        Effect = "Allow"
        Action = [
          "glue:BatchGetPartition",
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:GetPartition",
          "glue:GetPartitions",
          "glue:GetTable",
          "glue:GetTables",
        ]
        Resource = [
          "arn:aws:glue:*:*:catalog",
          aws_glue_catalog_database.cur.arn,
          "arn:aws:glue:*:*:table/${aws_glue_catalog_database.cur.name}/*",
        ]
      },
      {
        Sid    = "S3CurAndAthenaAccess"
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:ListBucketMultipartUploads",
          "s3:ListMultipartUploadParts",
          "s3:PutObject",
        ]
        Resource = [
          aws_s3_bucket.cur.arn,
          "${aws_s3_bucket.cur.arn}/*",
        ]
      },
      {
        Sid    = "KmsCurAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKey*",
        ]
        Resource = aws_kms_key.cur.arn
      },
    ]
  })
}
