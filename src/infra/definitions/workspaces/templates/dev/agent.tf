# Configures the primary Coder workspace agent resource, connection timeouts, and container startup lifecycle hooks.

resource "coder_agent" "main" {
  arch                    = var.workspace_arch
  os                      = "linux"
  connection_timeout      = 180
  startup_script_behavior = "blocking"

  env = {
    XDG_RUNTIME_DIR = "/home/coder/.runtime"
  }

  lifecycle {
    precondition {
      condition     = local.workspace_placement_inventory_valid
      error_message = "The published workspace placement inventory must contain only complete CPU, memory, storage, and accelerator resource envelopes."
    }

    precondition {
      condition     = local.workspace_incarnation_inventory_valid
      error_message = "The published workspace incarnation inventory must exactly cover the workspace placement inventory with valid immutable cell identifiers."
    }

    precondition {
      condition     = local.workspace_virtual_name_inventory_valid
      error_message = "The published workspace virtual-name inventory must exactly cover the workspace placement inventory with valid storage-gateway names."
    }

    precondition {
      condition = (
        contains(keys(local.workspace_placement_inventory), var.cell) &&
        contains(keys(local.workspace_placement_inventory), local.selected_cell)
      )
      error_message = "The selected cluster must be present in the published workspace placement inventory."
    }

    precondition {
      condition = try(
        tonumber(data.coder_parameter.cpu.value) == floor(tonumber(data.coder_parameter.cpu.value)) &&
        tonumber(data.coder_parameter.cpu.value) >= local.effective_workspace_cpu.min &&
        tonumber(data.coder_parameter.cpu.value) <= local.effective_workspace_cpu.max,
        false,
      )
      error_message = "Workspace CPU request must be a whole vCPU value inside the selected cluster's declared envelope."
    }

    precondition {
      condition = try(
        tonumber(data.coder_parameter.cpu_burst.value) == floor(tonumber(data.coder_parameter.cpu_burst.value)) &&
        tonumber(data.coder_parameter.cpu_burst.value) >= 0 &&
        local.workspace_cpu_limit <= local.effective_workspace_cpu.max,
        false,
      )
      error_message = "Workspace CPU burst must be a whole non-negative vCPU value not exceeding the cluster limit (${local.effective_workspace_cpu.max} vCPU)."
    }

    precondition {
      condition = try(
        tonumber(data.coder_parameter.memory_gib.value) == floor(tonumber(data.coder_parameter.memory_gib.value)) &&
        tonumber(data.coder_parameter.memory_gib.value) >= local.effective_workspace_memory.min &&
        tonumber(data.coder_parameter.memory_gib.value) <= local.effective_workspace_memory.max,
        false,
      )
      error_message = "Workspace memory request must be a whole GiB value inside the selected cluster's declared envelope."
    }

    precondition {
      condition = try(
        tonumber(data.coder_parameter.memory_burst_gib.value) == floor(tonumber(data.coder_parameter.memory_burst_gib.value)) &&
        tonumber(data.coder_parameter.memory_burst_gib.value) >= 0 &&
        local.workspace_memory_limit_gib <= local.effective_workspace_memory.max,
        false,
      )
      error_message = "Workspace memory burst must be a whole non-negative GiB value not exceeding the cluster limit (${local.effective_workspace_memory.max} GiB)."
    }

    precondition {
      condition = try(
        tonumber(data.coder_parameter.home_disk_gib.value) == floor(tonumber(data.coder_parameter.home_disk_gib.value)) &&
        tonumber(data.coder_parameter.home_disk_gib.value) >= local.workspace_storage.min &&
        tonumber(data.coder_parameter.home_disk_gib.value) <= local.workspace_storage.max,
        false,
      )
      error_message = "Workspace storage must be a whole GiB value inside the selected cluster's declared envelope."
    }

    precondition {
      condition = try(
        !local.is_ebs_storage ||
        local.disk_iops == null ||
        local.disk_throughput_mbps == null ||
        local.disk_iops >= 4 * local.disk_throughput_mbps,
        false,
      )
      error_message = "Disk IOPS must be at least 4 times sequential throughput in MB/s (AWS gp3 constraint: IOPS >= 4 * throughput)."
    }

    precondition {
      condition = try(
        !local.is_ebs_storage ||
        local.disk_iops == null ||
        local.disk_iops <= 500 * tonumber(data.coder_parameter.home_disk_gib.value),
        false,
      )
      error_message = "Disk IOPS must not exceed 500 times the disk size in GiB (AWS gp3 constraint: IOPS <= 500 * home_disk_gib)."
    }

    precondition {
      condition = (
        local.selected_accelerator_key == local.no_accelerator ||
        contains(keys(local.available_accelerator_offers), local.selected_accelerator_key)
      )
      error_message = "The selected accelerator must be available in the selected cluster."
    }

    precondition {
      condition = try(
        length(data.coder_parameter.accelerator_count) == (local.accelerator_count_configurable ? 1 : 0) && (
          local.selected_accelerator_offer == null ? local.accelerator_count == 0 : (
            local.accelerator_count == floor(local.accelerator_count) &&
            local.accelerator_count >= 1 &&
            local.accelerator_count <= local.selected_accelerator_offer.max_count &&
            (
              local.selected_accelerator_offer.kind != "tpu" ||
              local.accelerator_count == local.selected_accelerator_offer.max_count
            )
          )
        ),
        false,
      )
      error_message = "The accelerator count control must appear only for variable-count offers; its value must be whole, within the selected offer maximum, and use the complete TPU topology."
    }

    precondition {
      condition = (
        data.coder_parameter.ssh_enabled.value != "true" ||
        can(regex("^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-P][A-Za-z0-9+/]{42}( [^\\x00-\\x1F\\x7F]+)?$", data.coder_parameter.ssh_public_key.value))
      )
      error_message = "Enabling direct SSH requires one structurally valid OpenSSH Ed25519 public key."
    }

    precondition {
      condition = (
        local.template_preview || (
          local.workspace_origin_object != null &&
          try(local.workspace_origin_object.metadata.name, "") == "coder-workspace-origin" &&
          try(local.workspace_origin_object.metadata.namespace, "") == local.workspace_namespace &&
          tomap(local.workspace_origin_data) == tomap({
            authMode       = local.workload_origin_auth_mode
            cell           = local.selected_cell
            originRegion   = local.workload_origin_region
            originRegistry = local.workload_registry
            provider       = local.workload_origin_provider
            roleArn        = local.workload_origin_role_arn
            tokenAudience  = local.workload_origin_token_audience
            tokenFile      = local.workload_origin_token_file
          })
        )
      )
      error_message = "The template's workload-origin inputs must exactly match the selected cell's Argo-owned contract."
    }

    precondition {
      condition = (
        # floci-divergence: Floci uses local in-cluster workload origin registry without IAM credentials.
        local.workload_origin_auth_mode == "floci" ? (
          # floci-divergence: Floci uses local in-cluster workload origin registry without IAM credentials.
          local.workload_origin_provider == "floci" &&
          can(regex("^origin-registry:5000/[0-9]{12}/${local.workload_origin_region}$", local.workload_registry)) &&
          local.workload_registry_insecure == "false" &&
          local.workload_origin_role_arn == "" &&
          local.workload_origin_token_audience == "" &&
          local.workload_origin_token_file == ""
          ) : local.workload_origin_auth_mode == "eks-pod-identity" ? (
          local.workload_origin_provider == "aws" &&
          can(regex("^[0-9]{12}\\.dkr\\.ecr\\.${local.workload_origin_region}\\.amazonaws\\.com$", local.workload_registry)) &&
          local.workload_registry_insecure == "false" &&
          (local.workload_origin_role_arn == "" || can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]{1,64}$", local.workload_origin_role_arn))) &&
          local.workload_origin_token_audience == "" &&
          local.workload_origin_token_file == ""
          ) : (
          local.workload_origin_provider == "gcp" &&
          can(regex("^[0-9]{12}\\.dkr\\.ecr\\.${local.workload_origin_region}\\.amazonaws\\.com$", local.workload_registry)) &&
          local.workload_registry_insecure == "false" &&
          local.workload_origin_role_arn != "" &&
          local.workload_origin_token_audience == "sts.amazonaws.com" &&
          local.workload_origin_token_file == "/var/run/secrets/workload-origin/token"
        )
      )
      error_message = "Workload-origin identity, provider, transport, role, region, and token inputs must form one supported cell contract."
    }
  }

  display_apps {
    port_forwarding_helper = false
    ssh_helper             = false
    vscode                 = false
    vscode_insiders        = false
    web_terminal           = false
  }

  metadata {
    display_name = "CPU Usage"
    key          = "cpu"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 5
  }

  metadata {
    display_name = "Memory Usage"
    key          = "memory"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 5
  }

  metadata {
    display_name = "Workspace Disk"
    key          = "workspace-disk"
    script       = "coder stat disk --path $${HOME}"
    interval     = 60
    timeout      = 10
  }
}


resource "coder_script" "workspace_start" {
  agent_id           = coder_agent.main.id
  display_name       = "Setup"
  run_on_start       = true
  start_blocks_login = true
  timeout            = 3600
  script             = file("${path.module}/container/init/workspace-start.sh")
}

resource "coder_script" "workspace_mounts" {
  agent_id           = coder_agent.main.id
  display_name       = "Mounts"
  run_on_start       = true
  start_blocks_login = true
  timeout            = 60
  script             = file("${path.module}/container/init/workspace-mounts.sh")
}

resource "coder_script" "workspace_snapshots" {
  agent_id           = coder_agent.main.id
  display_name       = "Snapshots"
  run_on_start       = true
  start_blocks_login = local.is_new_restore
  timeout            = 3600
  script             = file("${path.module}/container/init/workspace-snapshots.sh")
}
