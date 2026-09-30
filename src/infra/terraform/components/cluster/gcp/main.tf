# Provisions private GKE clusters, Cloud KMS envelope encryption keys, and managed node pools.

data "google_client_config" "current" {}
data "google_project" "current" {}

locals {
  project  = coalesce(data.google_client_config.current.project, "default")
  location = coalesce(data.google_client_config.current.region, data.google_client_config.current.zone, "us-central1")
}

module "interface" {
  source = "../_interface"

  cluster_name                  = var.cluster_name
  vpc_id                        = var.vpc_id
  subnet_ids                    = var.subnet_ids
  kubernetes_version            = var.kubernetes_version
  enable_kms_secrets_encryption = var.enable_kms_secrets_encryption
  enable_control_plane_logging  = var.enable_control_plane_logging

  realized = {
    cluster_name      = google_container_cluster.this.name
    endpoint          = "https://${google_container_cluster.this.endpoint}"
    ca_certificate    = google_container_cluster.this.master_auth[0].cluster_ca_certificate
    oidc_issuer_url   = "https://container.googleapis.com/v1/${google_container_cluster.this.id}"
    oidc_provider_arn = null
  }
}

resource "google_kms_key_ring" "cluster" {
  count    = var.enable_kms_secrets_encryption ? 1 : 0
  name     = "${var.cluster_name}-cluster"
  location = local.location
  project  = local.project != "default" ? local.project : null
}

resource "google_kms_crypto_key" "cluster_secrets" {
  count           = var.enable_kms_secrets_encryption ? 1 : 0
  name            = "cluster-secrets"
  key_ring        = google_kms_key_ring.cluster[0].id
  rotation_period = "7776000s"
}

resource "google_kms_crypto_key_iam_member" "gke_cmek" {
  count         = var.enable_kms_secrets_encryption ? 1 : 0
  crypto_key_id = google_kms_crypto_key.cluster_secrets[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current.number}@container-engine-robot.iam.gserviceaccount.com"
}

resource "google_container_cluster" "this" {
  name     = module.interface.names.cluster
  location = local.location

  network    = var.vpc_id
  subnetwork = var.subnet_ids[0]

  min_master_version = var.kubernetes_version

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  datapath_provider = "ADVANCED_DATAPATH"

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = var.master_ipv4_cidr_block
  }

  dynamic "database_encryption" {
    for_each = var.enable_kms_secrets_encryption ? [1] : []
    content {
      state    = "ENCRYPTED"
      key_name = google_kms_crypto_key.cluster_secrets[0].id
    }
  }

  workload_identity_config {
    workload_pool = "${local.project}.svc.id.goog"
  }

  logging_config {
    enable_components = var.enable_control_plane_logging ? ["SYSTEM_COMPONENTS", "WORKLOADS", "APISERVER", "CONTROLLER_MANAGER", "SCHEDULER"] : ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  master_authorized_networks_config {}

  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  resource_labels = {
    component  = "cluster"
    managed_by = "opentofu"
  }

  node_config {
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.node_pool.email
    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
    ]
    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}

resource "google_service_account" "node_pool" {
  account_id   = substr(replace("${var.cluster_name}-node-pool", "_", "-"), 0, 28)
  display_name = "${var.cluster_name} GKE node pool service account"
  project      = local.project != "default" ? local.project : null
}

resource "google_project_iam_member" "node_pool" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  project = local.project != "default" ? local.project : null
  role    = each.key
  member  = "serviceAccount:${google_service_account.node_pool.email}"
}

resource "google_container_node_pool" "system" {
  name       = "system"
  cluster    = google_container_cluster.this.name
  location   = google_container_cluster.this.location
  node_count = 2

  autoscaling {
    min_node_count = 1
    max_node_count = 4
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = "e2-standard-4"
    service_account = google_service_account.node_pool.email
    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
    ]
    metadata = {
      disable-legacy-endpoints = "true"
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}
