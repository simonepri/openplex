# Tests node auto repair in the GKE cluster component.

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

  mock_resource "google_container_cluster" {
    defaults = {
      id       = "projects/mock-project/locations/us-central1/clusters/test-cluster"
      endpoint = "34.1.2.3"
    }
  }

  mock_resource "google_kms_key_ring" {
    defaults = {
      id = "projects/mock-project/locations/us-central1/keyRings/test-cluster-cluster"
    }
  }

  mock_resource "google_kms_crypto_key" {
    defaults = {
      id = "projects/mock-project/locations/us-central1/keyRings/test-cluster-cluster/cryptoKeys/cluster-secrets"
    }
  }

  mock_resource "google_service_account" {
    defaults = {
      email = "mock-sa@mock-project.iam.gserviceaccount.com"
    }
  }
}

override_resource {
  target = google_container_cluster.this
  values = {
    master_auth = {
      cluster_ca_certificate = "dGVzdC1jYQ=="
    }
  }
}

variables {
  cluster_name = "test-cluster"
  vpc_id       = "projects/mock/global/networks/test-vpc"
  subnet_ids   = ["projects/mock/regions/us-central1/subnetworks/test-subnet"]
}

run "verifies_system_node_pool_auto_repair" {
  command = plan

  assert {
    condition     = google_container_node_pool.system.management[0].auto_repair == true
    error_message = "System node pool must have auto_repair enabled."
  }
}
