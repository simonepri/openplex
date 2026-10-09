# Provisions per-team Cloudflare R2 storage buckets, lifecycles, and scoped API tokens.

locals {
  # Cloudflare permission groups "Workers R2 Storage Bucket Item Read" and "... Write".
  r2_bucket_item_read_permission  = "6a018a9f2fc74eb6b293b0c548f38b39"
  r2_bucket_item_write_permission = "2efd5506f9c8494dacb1fa10a3e7d5b6"
}

resource "cloudflare_r2_bucket" "this" {
  for_each   = var.teams
  account_id = var.account_id
  name       = "${var.name_prefix}-global-${each.key}-${var.account_suffix}"
  location   = var.location
}

resource "cloudflare_r2_bucket_lifecycle" "this" {
  for_each    = var.teams
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.this[each.key].name

  # Cloudflare returns rules sorted by ID; matching that order avoids reorder diffs.
  rules = [
    {
      id      = "abort-multipart-uploads"
      enabled = true
      conditions = {
        prefix = ""
      }
      abort_multipart_uploads_transition = {
        condition = {
          max_age = 604800
          type    = "Age"
        }
      }
    },
    {
      id      = "expire-scratch"
      enabled = true
      conditions = {
        prefix = "scratch/"
      }
      delete_objects_transition = {
        condition = {
          max_age = 2592000
          type    = "Age"
        }
      }
    }
  ]
}

resource "cloudflare_account_token" "this" {
  for_each   = var.teams
  account_id = var.account_id
  name       = "${var.name_prefix}-global-${each.key}-${var.account_suffix}"

  policies = [
    {
      effect = "allow"
      permission_groups = [
        {
          id = local.r2_bucket_item_read_permission
        },
        {
          id = local.r2_bucket_item_write_permission
        }
      ]
      resources = jsonencode({
        "com.cloudflare.edge.r2.bucket.${var.account_id}_default_${cloudflare_r2_bucket.this[each.key].name}" = "*"
      })
    }
  ]
}

resource "cloudflare_account_token" "reader" {
  for_each   = var.teams
  account_id = var.account_id
  name       = "${var.name_prefix}-global-${each.key}-reader-${var.account_suffix}"

  policies = [
    {
      effect = "allow"
      permission_groups = [
        {
          id = local.r2_bucket_item_read_permission
        }
      ]
      resources = jsonencode({
        "com.cloudflare.edge.r2.bucket.${var.account_id}_default_${cloudflare_r2_bucket.this[each.key].name}" = "*"
      })
    }
  ]
}
