# Provisions GCP BigQuery billing export datasets, OpenCost service accounts, and Workload Identity IAM bindings.

module "interface" {
  source = "../_interface"

  cluster_name = var.cluster_name
  project_id   = var.project_id
  realized = {
    bigquery_dataset = google_bigquery_dataset.billing_export.dataset_id
    role_arn         = google_service_account.opencost.email
  }
}

resource "google_bigquery_dataset" "billing_export" {
  dataset_id  = replace("${var.cluster_name}_billing_export", "-", "_")
  project     = var.project_id
  location    = var.gcp_region
  description = "GCP Cloud Billing export dataset for OpenCost reconciliation"
}

resource "google_service_account" "opencost" {
  account_id   = substr(replace("${var.cluster_name}-cost", "_", "-"), 0, 28)
  display_name = "${var.cluster_name} OpenCost service account"
  project      = var.project_id
}

resource "google_service_account_iam_binding" "workload_identity" {
  service_account_id = google_service_account.opencost.name
  role               = "roles/iam.workloadIdentityUser"
  members = [
    "serviceAccount:${var.project_id}.svc.id.goog[opencost/opencost]",
  ]
}

resource "google_bigquery_dataset_iam_member" "viewer" {
  dataset_id = google_bigquery_dataset.billing_export.dataset_id
  project    = var.project_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.opencost.email}"
}

resource "google_project_iam_member" "job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.opencost.email}"
}
