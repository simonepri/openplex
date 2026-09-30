# Provisions GCP Secret Manager secrets, initial secret versions, and IAM accessor bindings.

module "interface" {
  source = "../_interface"

  secret_name   = var.secret_name
  secret_values = var.secret_values
  realized = {
    secret_id  = google_secret_manager_secret.secret.secret_id
    secret_arn = google_secret_manager_secret.secret.id
  }
}

resource "google_secret_manager_secret" "secret" {
  secret_id = module.interface.names.secret

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "secret" {
  secret      = google_secret_manager_secret.secret.id
  secret_data = jsonencode(var.secret_values)
}
