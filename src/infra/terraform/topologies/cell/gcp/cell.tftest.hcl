# Tests team managed folders and Workload Identity integration in the GCP cell topology.

override_module {
  target = module.network_mesh
}

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

  mock_resource "google_compute_network" {
    defaults = {
      id = "projects/mock-project/global/networks/mock-vpc"
    }
  }

  mock_resource "google_compute_subnetwork" {
    defaults = {
      id = "projects/mock-project/regions/us-central1/subnetworks/mock-subnetwork"
    }
  }

  mock_resource "google_compute_router" {}
  mock_resource "google_compute_router_nat" {}

  mock_resource "google_container_cluster" {
    defaults = {
      id       = "projects/mock-project/locations/us-central1/clusters/cell-gcp-test"
      endpoint = "34.1.2.3"
    }
  }

  mock_resource "google_container_node_pool" {}

  mock_resource "google_kms_key_ring" {}
  mock_resource "google_kms_crypto_key" {
    defaults = {
      id = "projects/mock-project/locations/us/keyRings/cell-gcp-test-storage/cryptoKeys/storage-key"
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

  mock_resource "google_service_account" {
    defaults = {
      email = "mock-sa@mock-project.iam.gserviceaccount.com"
      name  = "projects/mock-project/serviceAccounts/mock-sa@mock-project.iam.gserviceaccount.com"
    }
  }
  mock_resource "google_service_account_iam_binding" {}
  mock_resource "google_project_iam_member" {}
  mock_resource "google_dns_managed_zone" {}
}

override_resource {
  target = module.cluster.google_container_cluster.this
  values = {
    master_auth = {
      cluster_ca_certificate = "dGVzdC1jYQ=="
    }
  }
}

variables {
  cluster_name       = "cell-gcp-test"
  availability_zones = ["us-central1-a", "us-central1-b", "us-central1-c"]
  tier_subnets = {
    private = ["10.1.0.0/20", "10.1.16.0/20", "10.1.32.0/20"]
    public  = ["10.1.48.0/24", "10.1.49.0/24", "10.1.50.0/24"]
    pod     = ["10.2.0.0/16", "10.3.0.0/16", "10.4.0.0/16"]
  }
  enable_dns          = false
  enable_network_mesh = false
}

run "verifies_cell_storage_team_managed_folders" {
  command = plan

  assert {
    condition     = module.storage.managed_folders.home["examples"].name == "home/examples/"
    error_message = "Home managed folder for examples team must be home/examples/."
  }

  assert {
    condition     = module.storage.managed_folders.scratch["examples"].name == "scratch/examples/"
    error_message = "Scratch managed folder for examples team must be scratch/examples/."
  }

  assert {
    condition     = module.storage.managed_folder_iam_bindings.home["examples"].role == "roles/storage.objectUser"
    error_message = "Folder-scoped IAM binding must use roles/storage.objectUser."
  }

  assert {
    condition     = module.storage.managed_folder_iam_bindings.scratch["examples"].role == "roles/storage.objectUser"
    error_message = "Folder-scoped IAM binding must use roles/storage.objectUser."
  }
}
