# Provisions Google service accounts, project IAM role bindings, and Workload Identity user permissions.

module "interface" {
  source = "../_interface"

  cluster_name             = var.cluster_name
  cluster_oidc_issuer_url  = var.cluster_oidc_issuer_url
  cluster_oidc_arn         = var.cluster_oidc_arn
  iam_name_prefix          = var.iam_name_prefix
  iam_permissions_boundary = var.iam_permissions_boundary
  project_id               = var.project_id
  roles                    = var.roles
  trust_mode               = var.trust_mode
  realized = {
    role_arns = {
      for k, v in google_service_account.this : k => v.email
    }
  }
}

resource "google_service_account" "this" {
  for_each = var.roles

  account_id = (
    length(replace(module.interface.names[each.key], "_", "-")) <= 30
    ? trimsuffix(replace(module.interface.names[each.key], "_", "-"), "-")
    : "${substr(replace(module.interface.names[each.key], "_", "-"), 0, 21)}-${substr(sha1(module.interface.names[each.key]), 0, 8)}"
  )
  display_name = "${var.cluster_name} ${each.key} service account"
  project      = var.project_id != "" ? var.project_id : null
}

resource "google_service_account_iam_binding" "this" {
  for_each = var.roles

  service_account_id = google_service_account.this[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  members = [
    "serviceAccount:${var.project_id}.svc.id.goog[${each.value.namespace}/${each.value.service_account}]",
  ]
}

data "google_client_config" "current" {}

locals {
  project = coalesce(var.project_id != "" ? var.project_id : null, data.google_client_config.current.project, "default")

  gcp_role_permissions = {
    # keep-sorted start block=yes
    atlantis = [
      { role = "roles/container.admin", condition = null },
      { role = "roles/compute.networkAdmin", condition = null },
      { role = "roles/compute.securityAdmin", condition = null },
      { role = "roles/storage.admin", condition = null },
      { role = "roles/secretmanager.admin", condition = null },
      { role = "roles/dns.admin", condition = null },
      {
        role = "roles/iam.serviceAccountAdmin"
        condition = {
          title       = "ScopedServiceAccountsOnly"
          description = "Limit service account administration to cluster-prefixed accounts"
          expression  = "resource.type == 'iam.googleapis.com/ServiceAccount' && resource.name.extract('serviceAccounts/{name}').startsWith('${var.cluster_name}')"
        }
      },
      { role = "roles/iam.roleAdmin", condition = null }
    ]
    barman = [
      {
        role = "roles/storage.objectAdmin"
        condition = {
          title       = "ScopedBackupBucketsOnly"
          description = "Limit storage access to cell backups bucket"
          expression  = "resource.name.startsWith('projects/_/buckets/') && resource.name.contains('${var.cluster_name}-backups')"
        }
      },
      {
        role = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
        condition = {
          title       = "ScopedStorageKMSOnly"
          description = "Limit KMS access to cell storage keys"
          expression  = "resource.name.contains('/keyRings/${var.cluster_name}-storage/cryptoKeys/')"
        }
      }
    ]
    cert-manager = [
      { role = "roles/dns.admin", condition = null }
    ]
    cert_manager = [
      { role = "roles/dns.admin", condition = null }
    ]
    cloud-telemetry = [
      { role = "roles/monitoring.metricWriter", condition = null },
      { role = "roles/cloudtrace.agent", condition = null },
      { role = "roles/logging.logWriter", condition = null },
      { role = "roles/pubsub.subscriber", condition = null },
    ]
    cloud_telemetry = [
      { role = "roles/monitoring.metricWriter", condition = null },
      { role = "roles/cloudtrace.agent", condition = null },
      { role = "roles/logging.logWriter", condition = null },
      { role = "roles/pubsub.subscriber", condition = null },
    ]
    coder-backups = [
      {
        role = "roles/storage.objectAdmin"
        condition = {
          title       = "ScopedBackupBucketsOnly"
          description = "Limit storage access to cell backups bucket"
          expression  = "resource.name.startsWith('projects/_/buckets/') && resource.name.contains('${var.cluster_name}-backups')"
        }
      },
      {
        role = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
        condition = {
          title       = "ScopedStorageKMSOnly"
          description = "Limit KMS access to cell storage keys"
          expression  = "resource.name.contains('/keyRings/${var.cluster_name}-storage/cryptoKeys/')"
        }
      }
    ]
    db-backups = [
      {
        role = "roles/storage.objectAdmin"
        condition = {
          title       = "ScopedBackupBucketsOnly"
          description = "Limit storage access to cell backups bucket"
          expression  = "resource.name.startsWith('projects/_/buckets/') && resource.name.contains('${var.cluster_name}-backups')"
        }
      },
      {
        role = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
        condition = {
          title       = "ScopedStorageKMSOnly"
          description = "Limit KMS access to cell storage keys"
          expression  = "resource.name.contains('/keyRings/${var.cluster_name}-storage/cryptoKeys/')"
        }
      }
    ]
    external-dns = [
      { role = "roles/dns.admin", condition = null }
    ]
    external_dns = [
      { role = "roles/dns.admin", condition = null }
    ]
    karpenter = [
      { role = "roles/compute.instanceAdmin.v1", condition = null },
      {
        role = "roles/iam.serviceAccountUser"
        condition = {
          title       = "ScopedNodeServiceAccountOnly"
          description = "Limit service account user to cluster node pool accounts"
          expression  = "resource.type == 'iam.googleapis.com/ServiceAccount' && resource.name.extract('serviceAccounts/{name}').startsWith('${var.cluster_name}')"
        }
      }
    ]
    kopia = [
      {
        role = "roles/storage.objectAdmin"
        condition = {
          title       = "ScopedBackupBucketsOnly"
          description = "Limit storage access to cell backups bucket"
          expression  = "resource.name.startsWith('projects/_/buckets/') && resource.name.contains('${var.cluster_name}-backups')"
        }
      },
      {
        role = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
        condition = {
          title       = "ScopedStorageKMSOnly"
          description = "Limit KMS access to cell storage keys"
          expression  = "resource.name.contains('/keyRings/${var.cluster_name}-storage/cryptoKeys/')"
        }
      }
    ]
    prowler = [
      {
        role      = "roles/iam.securityReviewer"
        condition = null
      },
      {
        role      = "roles/viewer"
        condition = null
      },
    ]
    velero = [
      {
        role = "roles/storage.objectAdmin"
        condition = {
          title       = "ScopedBackupBucketsOnly"
          description = "Limit storage access to cell backups bucket"
          expression  = "resource.name.startsWith('projects/_/buckets/') && resource.name.contains('${var.cluster_name}-backups')"
        }
      },
      {
        role = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
        condition = {
          title       = "ScopedStorageKMSOnly"
          description = "Limit KMS access to cell storage keys"
          expression  = "resource.name.contains('/keyRings/${var.cluster_name}-storage/cryptoKeys/')"
        }
      }
    ]
    # keep-sorted end
  }

  gcp_role_bindings = flatten([
    for role_key, role_data in var.roles : [
      for perm in lookup(local.gcp_role_permissions, role_key, []) : {
        role_key  = role_key
        gcp_role  = perm.role
        condition = perm.condition
      }
    ]
  ])
}

resource "google_project_iam_member" "scoped_permissions" {
  for_each = {
    for b in local.gcp_role_bindings : "${b.role_key}-${b.gcp_role}" => b
  }

  project = local.project != "default" ? local.project : null
  role    = each.value.gcp_role
  member  = "serviceAccount:${google_service_account.this[each.value.role_key].email}"

  dynamic "condition" {
    for_each = each.value.condition != null ? [each.value.condition] : []
    content {
      title       = condition.value.title
      description = condition.value.description
      expression  = condition.value.expression
    }
  }
}
