# Tests GCS bucket naming, per-team managed folders, and folder-scoped IAM bindings in the GCP storage component.

mock_provider "google" {
  mock_data "google_client_config" {
    defaults = {
      project = "mock-project"
      region  = "us-central1"
      zone    = "us-central1-a"
    }
  }

  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }

  mock_data "google_storage_project_service_account" {
    defaults = {
      email_address = "service-123456789012@gs-project-accounts.iam.gserviceaccount.com"
    }
  }

  mock_resource "google_kms_key_ring" {}
  mock_resource "google_kms_crypto_key" {
    defaults = {
      id = "projects/mock-project/locations/us/keyRings/cell-gcp-uscentral1-storage/cryptoKeys/storage-key"
    }
  }
  mock_resource "google_kms_crypto_key_iam_member" {}
  mock_resource "google_storage_bucket" {
    defaults = {
      url = "gs://mock-bucket"
    }
  }
  mock_resource "google_project_service" {}
  mock_resource "google_storage_bucket_iam_member" {}
  mock_resource "google_storage_insights_report_config" {}
  mock_resource "google_storage_managed_folder" {}
  mock_resource "google_storage_managed_folder_iam_binding" {}
}

variables {
  cluster_name = "cell-gcp-uscentral1"
  account_id   = "123456789012"
  teams        = ["alpha", "beta"]
  team_service_accounts = {
    alpha = "alpha-sa@mock-project.iam.gserviceaccount.com"
    beta  = "beta-sa@mock-project.iam.gserviceaccount.com"
  }
}

run "verifies_bucket_naming_and_managed_folders" {
  command = plan

  assert {
    condition     = google_storage_insights_report_config.this["home"].object_metadata_report_options[0].storage_destination_options[0].destination_path == "inventory/cell-gcp-uscentral1-home-123456789012/"
    error_message = "Each GCS inventory report must use a source-bucket-specific destination prefix."
  }

  assert {
    condition     = contains(google_storage_insights_report_config.this["home"].object_metadata_report_options[0].metadata_fields, "storageClass")
    error_message = "GCS inventory reports must include storageClass for s3i queries."
  }

  assert {
    condition     = google_storage_bucket.this["home"].name == "cell-gcp-uscentral1-home-123456789012"
    error_message = "Home bucket name must follow <cluster>-<tier>-<account_id> pattern."
  }

  assert {
    condition     = google_storage_bucket.this["scratch"].name == "cell-gcp-uscentral1-scratch-123456789012"
    error_message = "Scratch bucket name must follow <cluster>-<tier>-<account_id> pattern."
  }

  assert {
    condition     = google_storage_managed_folder.home["alpha"].name == "home/alpha/"
    error_message = "Home managed folder for alpha must be home/alpha/."
  }

  assert {
    condition     = google_storage_managed_folder.home["alpha"].bucket == "cell-gcp-uscentral1-home-123456789012"
    error_message = "Home managed folder for alpha must be in the home bucket."
  }

  assert {
    condition     = google_storage_managed_folder.scratch["alpha"].name == "scratch/alpha/"
    error_message = "Scratch managed folder for alpha must be scratch/alpha/."
  }

  assert {
    condition     = google_storage_managed_folder.scratch["alpha"].bucket == "cell-gcp-uscentral1-scratch-123456789012"
    error_message = "Scratch managed folder for alpha must be in the scratch bucket."
  }

  assert {
    condition     = google_storage_managed_folder_iam_binding.home["alpha"].role == "roles/storage.objectUser"
    error_message = "Folder-scoped IAM binding must use roles/storage.objectUser."
  }

  assert {
    condition     = contains(google_storage_managed_folder_iam_binding.home["alpha"].members, "serviceAccount:alpha-sa@mock-project.iam.gserviceaccount.com")
    error_message = "Folder-scoped IAM binding on home must include team service account."
  }

  assert {
    condition     = contains(google_storage_managed_folder_iam_binding.scratch["alpha"].members, "serviceAccount:alpha-sa@mock-project.iam.gserviceaccount.com")
    error_message = "Folder-scoped IAM binding on scratch must include team service account."
  }
}
