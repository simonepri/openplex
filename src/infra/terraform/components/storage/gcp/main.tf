# Provisions Google Cloud Storage buckets, Cloud KMS crypto keys, and uniform access controls.

data "google_client_config" "current" {}

locals {
  project    = coalesce(data.google_client_config.current.project, "default")
  kms_key_id = var.kms_key_name != "" ? var.kms_key_name : try(google_kms_crypto_key.storage[0].id, "")
}

module "interface" {
  source = "../_interface"

  installation_name = var.installation_name
  cell_name         = var.cell_name
  storage_tiers     = var.storage_tiers
  realized = {
    buckets = {
      for k, b in google_storage_bucket.this : k => {
        name = b.name
        url  = b.url
      }
    }
  }
}

resource "google_kms_key_ring" "storage" {
  count    = var.kms_key_name == "" ? 1 : 0
  name     = "${var.cell_name}-storage"
  location = lower(var.location)
  project  = local.project != "default" ? local.project : null
}

resource "google_kms_crypto_key" "storage" {
  count           = var.kms_key_name == "" ? 1 : 0
  name            = "storage-key"
  key_ring        = google_kms_key_ring.storage[0].id
  rotation_period = "7776000s"
}

data "google_storage_project_service_account" "gcs_account" {
  count   = var.kms_key_name == "" ? 1 : 0
  project = local.project != "default" ? local.project : null
}

resource "google_kms_crypto_key_iam_member" "storage" {
  count         = var.kms_key_name == "" ? 1 : 0
  crypto_key_id = google_kms_crypto_key.storage[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs_account[0].email_address}"
}

resource "google_storage_bucket" "this" {
  for_each = toset(var.storage_tiers)

  name                        = module.interface.names[each.key]
  location                    = var.location
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  dynamic "logging" {
    for_each = each.key != "logs" && contains(var.storage_tiers, "logs") ? [1] : []
    content {
      log_bucket        = module.interface.names["logs"]
      log_object_prefix = "${each.key}/"
    }
  }

  dynamic "retention_policy" {
    for_each = each.key == "archive" ? [1] : []
    content {
      retention_period = 2592000
    }
  }

  dynamic "lifecycle_rule" {
    for_each = each.key == "profiles" ? [var.profiles_retention_days] : contains(["scratch", "meta"], each.key) ? [30] : []
    content {
      action {
        type = "Delete"
      }
      condition {
        age                        = lifecycle_rule.value
        days_since_noncurrent_time = lifecycle_rule.value
      }
    }
  }

  encryption {
    default_kms_key_name = local.kms_key_id
  }

  depends_on = [google_kms_crypto_key_iam_member.storage]
}

resource "google_storage_insights_report_config" "this" {
  for_each = toset([for tier in var.storage_tiers : tier if contains(["home", "scratch", "meta", "backups", "archive"], tier)])

  project      = local.project != "default" ? local.project : null
  location     = var.location
  display_name = "${module.interface.names[each.key]}-inventory"

  frequency_options {
    frequency = "DAILY"

    start_date {
      day   = tonumber(formatdate("D", timeadd(timestamp(), "24h")))
      month = tonumber(formatdate("M", timeadd(timestamp(), "24h")))
      year  = tonumber(formatdate("YYYY", timeadd(timestamp(), "24h")))
    }

    end_date {
      day   = 31
      month = 12
      year  = 2050
    }
  }

  parquet_options {}

  object_metadata_report_options {
    metadata_fields = [
      "name",
      "size",
      "updated",
    ]

    storage_filters {
      bucket = google_storage_bucket.this[each.key].name
    }

    storage_destination_options {
      bucket           = google_storage_bucket.this["meta"].name
      destination_path = "inventory/"
    }
  }

  lifecycle {
    ignore_changes = [
      frequency_options[0].start_date,
    ]
  }

  depends_on = [
    google_project_service.storage_insights,
    google_storage_bucket_iam_member.insights_source,
    google_storage_bucket_iam_member.insights_destination,
  ]
}

resource "google_project_service" "storage_insights" {
  project                    = local.project != "default" ? local.project : null
  service                    = "storageinsights.googleapis.com"
  disable_dependent_services = false
  disable_on_destroy         = false
}

data "google_project" "current" {
  project_id = local.project != "default" ? local.project : null
}

resource "google_storage_bucket_iam_member" "insights_source" {
  for_each = toset([for tier in var.storage_tiers : tier if contains(["home", "scratch", "meta", "backups", "archive"], tier)])

  bucket = google_storage_bucket.this[each.key].name
  role   = "roles/storage.insightsCollectorService"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-storageinsights.iam.gserviceaccount.com"
}

resource "google_storage_bucket_iam_member" "insights_destination" {
  count = contains(var.storage_tiers, "meta") ? 1 : 0

  bucket = google_storage_bucket.this["meta"].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-storageinsights.iam.gserviceaccount.com"
}
