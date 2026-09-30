# Provisions GCP Compute Engine Tailscale router instances, service accounts, and Secret Manager tokens.

module "interface" {
  source = "../_interface"

  name              = var.name
  vpc_id            = var.vpc_id
  subnet_id         = var.subnet_id
  tailnet_auth_key  = var.tailnet_auth_key
  advertised_routes = var.advertised_routes
  realized = {
    instance_id = google_compute_instance.this.instance_id
    private_ip  = google_compute_instance.this.network_interface[0].network_ip
  }
}

data "google_client_config" "current" {}

locals {
  project = coalesce(data.google_client_config.current.project, "default")
}

resource "google_secret_manager_secret" "tailscale_auth_key" {
  secret_id = "${module.interface.names.instance}-tailscale-auth-key"
  project   = local.project != "default" ? local.project : null

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "tailscale_auth_key" {
  secret      = google_secret_manager_secret.tailscale_auth_key.id
  secret_data = var.tailnet_auth_key
}

resource "google_service_account" "router" {
  account_id   = substr(replace("${module.interface.names.instance}-router", "_", "-"), 0, 28)
  display_name = "Tailscale router service account for ${module.interface.names.instance}"
  project      = local.project != "default" ? local.project : null
}

resource "google_secret_manager_secret_iam_member" "router" {
  project   = google_secret_manager_secret.tailscale_auth_key.project
  secret_id = google_secret_manager_secret.tailscale_auth_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.router.email}"
}

resource "google_compute_instance" "this" {
  name           = module.interface.names.instance
  machine_type   = "e2-micro"
  can_ip_forward = true

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    block-project-ssh-keys = "true"
    enable-oslogin         = "true"
  }

  network_interface {
    subnetwork = var.subnet_id
  }

  service_account {
    email  = google_service_account.router.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = <<-EOF
    #!/bin/bash
    set -euo pipefail

    echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-tailscale.conf
    echo 'net.ipv6.conf.all.forwarding = 1' >> /etc/sysctl.d/99-tailscale.conf
    sysctl -p /etc/sysctl.d/99-tailscale.conf

    mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
    apt-get update
    apt-get install -y tailscale

    systemctl enable --now tailscaled

    ROUTES_FLAG=""
    if [ -n "${join(",", var.advertised_routes)}" ]; then
      ROUTES_FLAG="--advertise-routes=${join(",", var.advertised_routes)}"
    fi

    TOKEN=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
    AUTHKEY=$(curl -s -H "Authorization: Bearer $TOKEN" "https://secretmanager.googleapis.com/v1/${google_secret_manager_secret_version.tailscale_auth_key.name}:access" | sed -n 's/.*"data":"\([^"]*\)".*/\1/p' | base64 -d)

    tailscale up --authkey="$AUTHKEY" --hostname="${module.interface.names.instance}" $ROUTES_FLAG --accept-routes
  EOF

  tags = ["network-mesh-router"]
}
