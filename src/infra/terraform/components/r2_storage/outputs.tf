# Exports Cloudflare R2 storage endpoints and per-team S3-compatible credentials.

output "endpoint" {
  description = "Base endpoint URL for Cloudflare R2 storage."
  value       = "https://${var.account_id}.r2.cloudflarestorage.com"
}

output "teams" {
  description = "Map of team identifiers to R2 bucket details and S3-compatible credentials."
  value = {
    for team in var.teams : team => {
      access_key_id     = cloudflare_account_token.this[team].id
      bucket            = cloudflare_r2_bucket.this[team].name
      endpoint          = "https://${var.account_id}.r2.cloudflarestorage.com"
      secret_access_key = sha256(cloudflare_account_token.this[team].value)
    }
  }
  sensitive = true
}

output "readers" {
  description = "Map of team identifiers to R2 bucket details and S3-compatible read-only credentials."
  value = {
    for team in var.teams : team => {
      access_key_id     = cloudflare_account_token.reader[team].id
      bucket            = cloudflare_r2_bucket.this[team].name
      endpoint          = "https://${var.account_id}.r2.cloudflarestorage.com"
      secret_access_key = sha256(cloudflare_account_token.reader[team].value)
    }
  }
  sensitive = true
}
