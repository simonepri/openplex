# Provisions persistent volume claims, snapshot broker credentials, and storage lifecycle resources for workspaces.

locals {
  workspace_lineage_label = "${var.access_alias_domain}/workspace-lineage"
  snapshot_broker_url     = "https://${local.active_ctrl_name}-runtime-services.tailnet.${var.access_alias_domain}:8444"
  runtime_secret_name     = "coder-${data.coder_workspace.me.id}-runtime"
  backup_proxy_access_key = substr(upper(sha256(join("\u0000", [
    "workspace-backup-proxy-access-key",
    data.coder_workspace.me.id,
  ]))), 0, 20)
  backup_proxy_secret_key = sha256(join("\u0000", [
    "workspace-backup-proxy-secret-key",
    coder_agent.main.token,
  ]))
  backup_proxy_auth_key = format(
    "\"%s,%s\"",
    local.backup_proxy_access_key,
    local.backup_proxy_secret_key,
  )

  snapshot_selector_pattern = "^(?:cell-[a-z0-9][-a-z0-9]{0,61}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
  start_fresh_selector      = "__start-fresh__"

  home_volume_claim_name = module.coder_snapshots.home_volume_claim_name
  workspace_lineage      = module.coder_snapshots.workspace_lineage
  # tflint-ignore: terraform_unused_declarations
  restore_selector    = module.coder_snapshots.restore_selector
  restore_source_cell = module.coder_snapshots.restore_source_cell
  # tflint-ignore: terraform_unused_declarations
  restore_requested = module.coder_snapshots.restore_requested
  # tflint-ignore: terraform_unused_declarations
  is_new_restore              = module.coder_snapshots.is_new_restore
  is_cross_cell_restore       = local.restore_source_cell != "" && local.restore_source_cell != local.selected_cell
  restore_source_virtual_name = try(local.workspace_virtual_name_inventory[local.restore_source_cell], "")
  workspace_parent_lineage    = ""
  workspace_parent_snapshot   = module.coder_snapshots.restore_selector
  workspace_is_root           = !module.coder_snapshots.restore_requested

  # Inlined writer guard active object inventory & conflict checks (previously modules/workspace_writer_guard)
  writer_inventory_enabled = local.workspace_start_count == 1 && !local.template_preview
  writer_deployments = local.writer_inventory_enabled ? jsondecode(base64decode(
    data.external.workspace_writer_inventory[0].result.deployments
  )) : null
  writer_pods = local.writer_inventory_enabled ? jsondecode(base64decode(
    data.external.workspace_writer_inventory[0].result.pods
  )) : null
  writer_inventory_valid = !local.writer_inventory_enabled || try(
    local.writer_deployments.apiVersion == "apps/v1" &&
    local.writer_deployments.kind == "DeploymentList" &&
    (local.writer_deployments.items == null || can(concat(local.writer_deployments.items, []))) &&
    local.writer_pods.apiVersion == "meta.k8s.io/v1" &&
    local.writer_pods.kind == "PartialObjectMetadataList" &&
    (local.writer_pods.items == null || can(concat(local.writer_pods.items, []))),
    false,
  )
  active_owner_deployment_objects = [
    for deployment in try(local.writer_deployments.items[*], []) : {
      lineage = try(deployment.metadata.labels[local.workspace_lineage_label], "")
      metadata = {
        labels = try(deployment.metadata.labels, {})
        name   = try(deployment.metadata.name, "")
      }
      spec = { replicas = try(deployment.spec.replicas, 1) }
    }
  ]
  active_owner_pod_objects = [
    for pod in try(local.writer_pods.items[*], []) : {
      lineage = try(pod.metadata.labels[local.workspace_lineage_label], "")
      metadata = {
        labels = try(pod.metadata.labels, {})
        name   = try(pod.metadata.name, "")
      }
      status = { phase = "Running" }
    }
  ]
  writer_candidates = concat(
    [for deployment in local.active_owner_deployment_objects : {
      active       = try(deployment.spec.replicas, 1) > 0
      kind         = "Deployment"
      lineage      = try(deployment.lineage, "")
      name         = try(deployment.metadata.name, "")
      workspace_id = try(deployment.metadata.labels["com.coder.workspace.id"], "")
    }],
    [for pod in local.active_owner_pod_objects : {
      active       = true
      kind         = "Pod"
      lineage      = try(pod.lineage, "")
      name         = try(pod.metadata.name, "")
      workspace_id = try(pod.metadata.labels["com.coder.workspace.id"], "")
    }],
  )
  workspace_writer_conflicts = [
    for candidate in local.writer_candidates : "${candidate.kind}/${candidate.name}"
    if candidate.workspace_id != data.coder_workspace.me.id && candidate.active && (
      (!can(regex("^[0-9a-f-]{36}-[0-9]{10,}$", candidate.lineage)) && !can(regex("^[0-9a-f]{40}$", candidate.lineage))) ||
      candidate.lineage == local.workspace_lineage
    )
  ]
}

module "coder_snapshots" {
  source = "../../modules/coder_snapshots"

  agent_id                    = coder_agent.main.id
  app_labels                  = local.app_labels
  home_disk_gib               = data.coder_parameter.home_disk_gib.value
  kopia_repository_bucket     = "repository"
  lineage_input               = can(regex("^[0-9]{10,}$", data.external.workspace_build_context.result.timestamp)) ? "${data.coder_workspace.me.id}-${data.external.workspace_build_context.result.timestamp}" : data.coder_workspace.me.name
  max_bandwidth_mbps          = 0
  owner_id                    = local.owner_id
  owner_username              = local.owner_username
  restore_selector            = data.coder_parameter.restore_selector.value
  s3_endpoint                 = "http://127.0.0.1:19847"
  single_writer_guard_enabled = true
  snapshot_interval           = "0 */30 * * * *"
  storage_class_name          = var.storage_class_name
  target_dir                  = "/var/lib/workspace"
  team                        = var.team
  workspace_id                = data.coder_workspace.me.id
  workspace_name              = data.coder_workspace.me.name
  workspace_namespace         = local.workspace_namespace
}

data "external" "workspace_writer_inventory" {
  count = local.writer_inventory_enabled ? 1 : 0
  program = [
    "${path.module}/hooks/workspace-writer-inventory.sh",
    var.kubernetes_config_path,
    local.workspace_namespace,
    local.owner_id,
    local.selected_cell,
  ]
}

resource "kubernetes_secret_v1" "workspace_runtime" {
  count = local.workspace_start_count

  metadata {
    name      = local.runtime_secret_name
    namespace = local.workspace_namespace
    labels = merge(local.app_labels, {
      "app.kubernetes.io/component" = "runtime-credentials"
      "app.kubernetes.io/name"      = "coder-workspace-runtime"
    })
  }

  data = {
    backup_proxy_access_key = local.backup_proxy_access_key
    backup_proxy_auth_key   = local.backup_proxy_auth_key
    backup_proxy_secret_key = local.backup_proxy_secret_key
    coder_agent_token       = coder_agent.main.token
    coder_session_token     = data.coder_workspace_owner.me.session_token
    zasper_access_token     = local.zasper_access_token
  }
}

moved {
  from = kubernetes_persistent_volume_claim_v1.home
  to   = module.coder_snapshots.kubernetes_persistent_volume_claim_v1.home
}

moved {
  from = coder_script.hourly_snapshot
  to   = module.coder_snapshots.coder_script.hourly_snapshot
}

moved {
  from = coder_script.shutdown_snapshot
  to   = module.coder_snapshots.coder_script.shutdown_snapshot
}

moved {
  from = terraform_data.applied_restore_selector
  to   = module.coder_snapshots.terraform_data.applied_restore_selector
}

moved {
  from = terraform_data.restore_generation
  to   = module.coder_snapshots.terraform_data.restore_generation
}

moved {
  from = terraform_data.workspace_lineage
  to   = module.coder_snapshots.terraform_data.workspace_lineage
}

