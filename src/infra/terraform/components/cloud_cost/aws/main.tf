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
    athena_table     = aws_glue_catalog_table.cur.name
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

resource "aws_kms_alias" "cur" {
  name          = "alias/${var.kms_alias_prefix}${var.cluster_name}-billing"
  target_key_id = aws_kms_key.cur.key_id
}

resource "aws_s3_bucket" "cur" {
  bucket = "${var.cluster_name}-billing-reports-${var.account_id}"
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
  bucket = "${var.cluster_name}-billing-access-logs-${var.account_id}"
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

resource "aws_s3_bucket_policy" "cur_access_logs" {
  bucket = aws_s3_bucket.cur_access_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnforceTLSRequestsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.cur_access_logs.arn,
          "${aws_s3_bucket.cur_access_logs.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid    = "AllowS3LogDelivery"
        Effect = "Allow"
        Principal = {
          Service = "logging.s3.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.cur_access_logs.arn}/*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = var.account_id
          }
        }
      },
    ]
  })
}

resource "aws_s3_bucket_logging" "cur" {
  bucket        = aws_s3_bucket.cur.id
  target_bucket = aws_s3_bucket.cur_access_logs.id
  target_prefix = "s3-access-logs/cur/"
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
  name                 = "${var.iam_name_prefix}${var.cluster_name}-opencost"
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
        Sid    = "EC2SpotPriceHistory"
        Effect = "Allow"
        Action = [
          "ec2:DescribeSpotPriceHistory",
        ]
        Resource = "*"
      },
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

locals {
  # Glue names the table after the last path segment, with hyphens replaced by underscores.
  cur_data_prefix = "${aws_cur_report_definition.cur.s3_prefix}/${aws_cur_report_definition.cur.report_name}/${aws_cur_report_definition.cur.report_name}/"
}

resource "aws_iam_role" "crawler" {
  name                 = "${var.iam_name_prefix}${var.cluster_name}-cur-crawler"
  permissions_boundary = var.iam_permissions_boundary

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "glue.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "crawler_glue_service" {
  role       = aws_iam_role.crawler.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "crawler" {
  name = "cur-data-read"
  role = aws_iam_role.crawler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadCurData"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.cur.arn}/${local.cur_data_prefix}*"
      },
      {
        Sid      = "ListCurBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.cur.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["${local.cur_data_prefix}*"]
          }
        }
      },
      {
        Sid      = "DecryptCurData"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = aws_kms_key.cur.arn
      },
    ]
  })
}

resource "aws_glue_catalog_table" "cur" {
  name          = replace("${var.cluster_name}_cur", "-", "_")
  database_name = aws_glue_catalog_database.cur.name
  table_type    = "EXTERNAL_TABLE"

  # The crawler refuses to merge columns or partitions into a table whose
  # classification parameters differ from the ones it detects for Parquet.
  parameters = {
    "EXTERNAL"            = "TRUE"
    "classification"      = "parquet"
    "compressionType"     = "none"
    "parquet.compression" = "SNAPPY"
    "typeOfData"          = "file"
  }

  partition_keys {
    name = "year"
    type = "string"
  }

  partition_keys {
    name = "month"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.cur.id}/${local.cur_data_prefix}"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet-serde"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "identity_line_item_id"
      type = "string"
    }
    columns {
      name = "identity_time_interval"
      type = "string"
    }
    columns {
      name = "bill_payer_account_id"
      type = "string"
    }
    columns {
      name = "bill_billing_period_start_date"
      type = "timestamp"
    }
    columns {
      name = "bill_billing_period_end_date"
      type = "timestamp"
    }
    columns {
      name = "line_item_usage_account_id"
      type = "string"
    }
    columns {
      name = "line_item_line_item_type"
      type = "string"
    }
    columns {
      name = "line_item_usage_start_date"
      type = "timestamp"
    }
    columns {
      name = "line_item_usage_end_date"
      type = "timestamp"
    }
    columns {
      name = "line_item_product_code"
      type = "string"
    }
    columns {
      name = "line_item_usage_type"
      type = "string"
    }
    columns {
      name = "line_item_operation"
      type = "string"
    }
    columns {
      name = "line_item_availability_zone"
      type = "string"
    }
    columns {
      name = "line_item_resource_id"
      type = "string"
    }
    columns {
      name = "line_item_usage_amount"
      type = "double"
    }
    columns {
      name = "line_item_normalization_factor"
      type = "double"
    }
    columns {
      name = "line_item_normalized_usage_amount"
      type = "double"
    }
    columns {
      name = "line_item_currency_code"
      type = "string"
    }
    columns {
      name = "line_item_unblended_rate"
      type = "string"
    }
    columns {
      name = "line_item_unblended_cost"
      type = "double"
    }
    columns {
      name = "line_item_blended_rate"
      type = "string"
    }
    columns {
      name = "line_item_blended_cost"
      type = "double"
    }
    columns {
      name = "line_item_net_unblended_rate"
      type = "string"
    }
    columns {
      name = "line_item_net_unblended_cost"
      type = "double"
    }
    columns {
      name = "pricing_public_on_demand_cost"
      type = "double"
    }
    columns {
      name = "pricing_public_on_demand_rate"
      type = "string"
    }
    columns {
      name = "pricing_unit"
      type = "string"
    }
    columns {
      name = "product_product_family"
      type = "string"
    }
    columns {
      name = "product_product_name"
      type = "string"
    }
    columns {
      name = "product_instance_type"
      type = "string"
    }
    columns {
      name = "product_region"
      type = "string"
    }
    columns {
      name = "product_servicecode"
      type = "string"
    }
    columns {
      name = "reservation_reservation_a_r_n"
      type = "string"
    }
    columns {
      name = "reservation_effective_cost"
      type = "double"
    }
    columns {
      name = "reservation_start_time"
      type = "string"
    }
    columns {
      name = "reservation_end_time"
      type = "string"
    }
    columns {
      name = "reservation_number_of_reservations"
      type = "string"
    }
    columns {
      name = "reservation_total_reserved_units"
      type = "string"
    }
    columns {
      name = "reservation_units_per_reservation"
      type = "string"
    }
    columns {
      name = "savings_plan_savings_plan_a_r_n"
      type = "string"
    }
    columns {
      name = "savings_plan_savings_plan_rate"
      type = "double"
    }
    columns {
      name = "savings_plan_savings_plan_effective_cost"
      type = "double"
    }
    columns {
      name = "savings_plan_total_commitment_to_date"
      type = "double"
    }
    columns {
      name = "savings_plan_used_commitment"
      type = "double"
    }
    columns {
      name = "savings_plan_payment_option"
      type = "string"
    }
    columns {
      name = "savings_plan_purchase_term"
      type = "string"
    }
    columns {
      name = "savings_plan_start_time"
      type = "string"
    }
    columns {
      name = "savings_plan_end_time"
      type = "string"
    }
  }

  lifecycle {
    # The crawler owns these fields and rewrites them on every run.
    ignore_changes = [
      owner,
      parameters,
      storage_descriptor[0].columns,
      storage_descriptor[0].number_of_buckets,
      storage_descriptor[0].parameters,
      storage_descriptor[0].ser_de_info,
    ]
  }
}

resource "aws_glue_crawler" "cur" {
  name          = "${var.cluster_name}-cur"
  database_name = aws_glue_catalog_database.cur.name
  role          = aws_iam_role.crawler.arn
  schedule      = "cron(0 6 * * ? *)"

  catalog_target {
    database_name = aws_glue_catalog_database.cur.name
    tables        = [aws_glue_catalog_table.cur.name]
  }

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }

  configuration = jsonencode({
    Version  = 1.0
    Grouping = { TableGroupingPolicy = "CombineCompatibleSchemas" }
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
      Tables     = { AddOrUpdateBehavior = "MergeNewColumns" }
    }
  })
}
