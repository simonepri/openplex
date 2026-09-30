# Provisions GCP Artifact Registry Docker repositories, KMS keys, and cleanup policies.

module "interface" {
  source            = "../_interface"
  installation_name = var.installation_name
  repositories      = var.repositories
  realized = {
    registry_url = "${google_artifact_registry_repository.this.location}-docker.pkg.dev"
    repositories = {
      for r in var.repositories : r => "${google_artifact_registry_repository.this.location}-docker.pkg.dev/${google_artifact_registry_repository.this.project}/${google_artifact_registry_repository.this.repository_id}/${r}"
    }
  }
}

resource "google_artifact_registry_repository" "this" {
  repository_id = var.installation_name
  format        = "DOCKER"
  description   = "Container image registry for ${var.installation_name}"

  docker_config {
    immutable_tags = true
  }
}
