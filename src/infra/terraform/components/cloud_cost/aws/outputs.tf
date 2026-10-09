# Exports AWS Cost and Usage Report S3 bucket names, bucket ARNs, and KMS key ARNs.

output "record" {
  description = "Canonical cloud_cost record containing billing export and IAM details."
  value       = module.interface.record
}

output "crawler_name" {
  description = "Name of the Glue crawler that creates and refreshes the CUR table; run it once after the first report delivery."
  value       = aws_glue_crawler.cur.name
}

output "table_name" {
  description = "Name of the Glue catalog table for CUR queries."
  value       = aws_glue_catalog_table.cur.name
}

output "athena_table" {
  description = "Name of the Glue catalog table for CUR queries."
  value       = aws_glue_catalog_table.cur.name
}
