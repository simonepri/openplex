# Exports active restore generation identifiers, snapshot sync script paths, and volume binding outputs.

output "active_restore_generation" {
  description = "Active restore generation identifier used for volume suffixing."
  value       = local.active_restore_generation
}

output "environment_variables" {
  description = "Environment variables to inject into the workspace container for snapshot management."
  value = {
    # keep-sorted start
    CODER_WORKSPACE_NAME      = var.workspace_name
    CODER_WORKSPACE_OWNER_ID  = var.owner_id
    KOPIA_MAX_BANDWIDTH_MBPS  = tostring(var.max_bandwidth_mbps)
    KOPIA_REPOSITORY_BUCKET   = var.kopia_repository_bucket
    KOPIA_RESTORE_SELECTOR    = local.restore_selector
    KOPIA_RESTORE_SOURCE_CELL = local.restore_source_cell
    KOPIA_S3_ENDPOINT         = var.s3_endpoint
    TARGET_DIR                = var.target_dir
    WORKSPACE_LINEAGE         = local.workspace_lineage
    WORKSPACE_PARENT_SNAPSHOT = local.restore_selector
    # keep-sorted end
  }
}

output "is_new_restore" {
  description = "Whether this plan execution represents a new restore operation."
  value       = local.is_new_restore
}

output "home_volume_claim_annotations" {
  description = "Annotations applied to the Kubernetes PersistentVolumeClaim created for the home volume."
  value       = kubernetes_persistent_volume_claim_v1.home.metadata[0].annotations
}

output "home_volume_claim_name" {
  description = "Name of the Kubernetes PersistentVolumeClaim created for the home volume."
  value       = kubernetes_persistent_volume_claim_v1.home.metadata[0].name
}

output "restore_requested" {
  description = "Whether a snapshot restore has been requested."
  value       = local.restore_requested
}

output "restore_selector" {
  description = "Snapshot selector ID to restore (stripped of source cell prefix)."
  value       = local.restore_selector
}

output "restore_script" {
  description = "Content of the kopia restore script."
  value       = file("${path.module}/scripts/kopia-restore.sh")
}

output "restore_source_cell" {
  description = "Source cell identifier if the restore is from an external cell."
  value       = local.restore_source_cell
}

output "workspace_lineage" {
  description = "Derived lineage token representing the durable provenance claim of the workspace."
  value       = local.workspace_lineage
}
