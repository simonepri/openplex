# Manages Coder workspace snapshot lifecycles, generation volume allocation, and Kopia sync/restore triggers.

locals {
  # tflint-ignore: terraform_unused_declarations
  snapshot_selector_pattern = "^(?:cell-[a-z0-9][-a-z0-9]{0,61}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
  start_fresh_selector      = "__start-fresh__"
  raw_restore_selector      = var.restore_selector == local.start_fresh_selector ? "" : var.restore_selector
  restore_selector_parts    = split("/", local.raw_restore_selector)
  restore_has_source_cell   = length(local.restore_selector_parts) > 1
  restore_source_cell       = local.restore_has_source_cell ? local.restore_selector_parts[0] : ""
  restore_selector          = local.restore_has_source_cell ? join("/", slice(local.restore_selector_parts, 1, length(local.restore_selector_parts))) : local.raw_restore_selector
  restore_requested         = local.restore_selector != ""
  applied_restore_selector  = try(terraform_data.applied_restore_selector.output, "")
  is_new_restore            = local.restore_requested && local.applied_restore_selector != local.restore_selector
  restore_generation        = local.restore_requested ? substr(sha256(local.restore_selector), 0, 8) : "0"
  active_restore_generation = local.is_new_restore ? local.restore_generation : try(terraform_data.restore_generation.output, "0")

  previous_disk_gib      = try(terraform_data.applied_disk_gib.output, var.home_disk_gib)
  is_disk_reduction      = var.home_disk_gib < local.previous_disk_gib
  disk_generation        = local.is_disk_reduction ? substr(sha256("${var.workspace_id}-${var.home_disk_gib}"), 0, 8) : "0"
  active_disk_gen        = local.is_disk_reduction ? local.disk_generation : try(terraform_data.disk_generation.output, "0")
  volume_suffix          = local.active_disk_gen != "0" ? "-g${local.active_disk_gen}" : ""
  home_volume_claim_name = "coder-${var.workspace_id}-home${local.volume_suffix}"

  # Lineage token derivation
  canonical_lineage = var.lineage_input != "" ? var.lineage_input : var.workspace_id
  requested_workspace_lineage = (
    can(regex("^[0-9a-f-]{36}-[0-9]{10,}$", local.canonical_lineage)) || can(regex("^[0-9a-f]{40}$", local.canonical_lineage))
    ) ? local.canonical_lineage : substr(sha256(join("\u0000", [
      "workspace-lineage",
      var.owner_id,
      local.canonical_lineage,
  ])), 0, 40)
  persisted_lineage_input = try(terraform_data.workspace_lineage.output, "")
  workspace_lineage = local.is_new_restore ? local.requested_workspace_lineage : (
    (can(regex("^[0-9a-f-]{36}-[0-9]{10,}$", local.persisted_lineage_input)) || can(regex("^[0-9a-f]{40}$", local.persisted_lineage_input))) ? local.persisted_lineage_input : (
      local.persisted_lineage_input != "" ? substr(sha256(join("\u0000", [
        "workspace-lineage",
        var.owner_id,
        local.persisted_lineage_input,
      ])), 0, 40) : local.requested_workspace_lineage
    )
  )

  ebs_annotations = merge(
    var.disk_iops != null ? { "ebs.csi.aws.com/iops" = tostring(var.disk_iops) } : {},
    var.disk_throughput_mbps != null ? { "ebs.csi.aws.com/throughput" = tostring(var.disk_throughput_mbps) } : {},
  )
}

resource "terraform_data" "applied_restore_selector" {
  input = local.restore_selector
}

resource "terraform_data" "applied_disk_gib" {
  input = var.home_disk_gib
}

resource "terraform_data" "disk_generation" {
  input = local.disk_generation

  lifecycle {
    ignore_changes = [input]
    replace_triggered_by = [
      terraform_data.applied_disk_gib,
    ]
  }
}

resource "terraform_data" "restore_generation" {
  input = local.restore_generation

  lifecycle {
    ignore_changes = [input]
    replace_triggered_by = [
      terraform_data.applied_restore_selector,
    ]
  }
}

resource "terraform_data" "workspace_lineage" {
  input = local.requested_workspace_lineage

  lifecycle {
    ignore_changes = [input]
    replace_triggered_by = [
      terraform_data.applied_restore_selector,
    ]
  }
}

resource "kubernetes_persistent_volume_claim_v1" "home" {
  metadata {
    name        = local.home_volume_claim_name
    namespace   = var.workspace_namespace
    labels      = var.app_labels
    annotations = local.ebs_annotations
  }

  wait_until_bound = false

  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = var.storage_class_name

    resources {
      requests = {
        storage = "${var.home_disk_gib}Gi"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = !local.is_disk_reduction || local.restore_requested
      error_message = "Decreasing workspace disk size requires an explicit backup selector. Copy a verified backup selector into 'Restore from backup' before decreasing the disk size."
    }
  }
}

resource "coder_script" "hourly_snapshot" {
  agent_id     = var.agent_id
  cron         = var.snapshot_interval
  display_name = "Snapshots"
  timeout      = 1800
  script       = file("${path.module}/scripts/kopia-sync.sh")
}

resource "coder_script" "shutdown_snapshot" {
  agent_id     = var.agent_id
  display_name = "Back up workspace before stopping"
  run_on_stop  = true
  # Must finish inside the pod's termination grace period, which bounds how long
  # a deleted workspace holds its queue quota and blocks its volume teardown.
  timeout = 90
  script  = file("${path.module}/scripts/kopia-sync.sh")
}
