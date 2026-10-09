# Tests Google service accounts, Workload Identity bindings, and account ID generation in the GCP identity component.

mock_provider "google" {
  mock_data "google_client_config" {
    defaults = {
      project = "mock-project"
    }
  }

  mock_resource "google_service_account" {
    defaults = {
      email = "mock-sa@mock-project.iam.gserviceaccount.com"
      name  = "projects/mock-project/serviceAccounts/mock-sa@mock-project.iam.gserviceaccount.com"
    }
  }

  mock_resource "google_service_account_iam_binding" {}
  mock_resource "google_project_iam_member" {}
}

variables {
  cluster_name            = "cell-gcp-uscentral1"
  cluster_oidc_issuer_url = "https://container.googleapis.com/v1/projects/mock-project/locations/us-central1/clusters/cell-gcp-uscentral1"
  project_id              = "mock-project"

  roles = {
    external-dns = {
      namespace       = "external-dns-system"
      service_account = "external-dns"
    }
    s3-gateway-alpha = {
      namespace       = "s3-system"
      service_account = "s3-gateway-alpha"
    }
    s3-gateway-beta = {
      namespace       = "s3-system"
      service_account = "s3-gateway-beta"
    }
    cloud-telemetry = {
      namespace       = "otel-system"
      service_account = "cloud-telemetry"
    }
  }
}

run "verifies_service_account_ids_and_length" {
  command = plan

  assert {
    condition     = length(google_service_account.this["s3-gateway-alpha"].account_id) <= 30
    error_message = "GCP service account ID must not exceed 30 characters."
  }

  assert {
    condition     = length(google_service_account.this["s3-gateway-beta"].account_id) <= 30
    error_message = "GCP service account ID must not exceed 30 characters."
  }

  assert {
    condition     = google_service_account.this["s3-gateway-alpha"].account_id != google_service_account.this["s3-gateway-beta"].account_id
    error_message = "Different team service accounts must have distinct account IDs."
  }
}

run "verifies_cloud_telemetry_workload_identity" {
  command = plan

  assert {
    condition     = contains(google_service_account_iam_binding.this["cloud-telemetry"].members, "serviceAccount:mock-project.svc.id.goog[otel-system/cloud-telemetry]")
    error_message = "cloud-telemetry Workload Identity binding must point to otel-system/cloud-telemetry."
  }

  assert {
    condition     = google_project_iam_member.scoped_permissions["cloud-telemetry-roles/pubsub.subscriber"].role == "roles/pubsub.subscriber"
    error_message = "cloud-telemetry must have roles/pubsub.subscriber assigned."
  }
}
