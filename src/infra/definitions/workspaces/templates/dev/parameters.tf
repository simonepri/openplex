# Defines user-facing Coder template parameters for resource sizing, GPU allocation, and snapshot restore points.

data "coder_parameter" "cluster" {
  name         = "cluster"
  display_name = "Cluster"
  description  = "Authorized cluster where this workspace runs. Its eligible node pools determine the resource choices below."
  default      = var.cell
  form_type    = "dropdown"
  mutable      = false
  order        = 10

  dynamic "option" {
    for_each = sort(keys(local.workspace_placement_inventory))
    content {
      name  = option.value
      value = option.value
    }
  }
}

data "coder_parameter" "accelerator" {
  name         = "accelerator"
  display_name = "Accelerators"
  description  = "Optional accelerator type and capacity. Choices reflect what the selected cluster can run."
  default      = local.no_accelerator
  form_type    = "dropdown"
  mutable      = true
  order        = 20
  styling      = jsonencode({ disabled = length(local.available_accelerator_offers) == 0 })

  option {
    name  = "None"
    value = local.no_accelerator
  }

  dynamic "option" {
    for_each = sort(keys(local.available_accelerator_offers))
    content {
      description = local.available_accelerator_offers[option.value].description
      name        = local.available_accelerator_offers[option.value].display_name
      value       = option.value
    }
  }
}

data "coder_parameter" "accelerator_count" {
  # LINT.IfChange(no-accelerator-sentinel)
  count = data.coder_parameter.accelerator.value == "__none__" ? 0 : (
    local.accelerator_count_configurable ? 1 : 0
  )
  # LINT.ThenChange(//src/infra/definitions/workspaces/templates/dev/accelerators.tf:no-accelerator-sentinel)

  name         = "accelerator_count"
  display_name = "Accelerator count"
  description  = "Number of identical accelerators attached to the workspace."
  default      = "1"
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 21

  validation {
    min = 1
    max = local.selected_accelerator_offer.max_count
  }
}

data "coder_parameter" "cpu" {
  name         = "cpu"
  display_name = "CPU"
  description  = "Workspace guaranteed CPU request in vCPU."
  default      = tostring(local.workspace_cpu.default)
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 22

  validation {
    min = local.workspace_cpu.min
    max = local.workspace_cpu.max
  }
}

data "coder_parameter" "cpu_burst" {
  name         = "cpu_burst"
  display_name = "CPU (Burst)"
  description  = "Workspace burstable CPU headroom above base in vCPU. Bursts to idle host cores without CFS throttling."
  default      = tostring(max(0, min(64 - local.workspace_cpu.default, local.workspace_cpu.max - local.workspace_cpu.default)))
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 23

  validation {
    min = 0
    max = local.workspace_cpu.max - local.workspace_cpu.min
  }
}

data "coder_parameter" "memory_gib" {
  name         = "memory_gib"
  display_name = "Memory"
  description  = "Workspace guaranteed RAM request in GiB."
  default      = tostring(local.workspace_memory.default)
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 24

  validation {
    min = local.workspace_memory.min
    max = local.workspace_memory.max
  }
}

data "coder_parameter" "memory_burst_gib" {
  name         = "memory_burst_gib"
  display_name = "Memory (Burst)"
  description  = "Workspace burstable memory headroom above RAM in GiB, backed by host swap."
  default      = tostring(max(0, min(8, local.workspace_memory.max - local.workspace_memory.default)))
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 25

  validation {
    min = 0
    max = local.workspace_memory.max - local.workspace_memory.min
  }
}

data "coder_parameter" "home_disk_gib" {
  name         = "home_disk_gib"
  display_name = "Workspace disk"
  description  = "Persistent disk for the home and repository directories. Reducing disk size requires an explicit backup selector in 'Restore from backup'.${local.is_ebs_storage ? "\n\nMonthly cost: $0.08 per GiB (e.g. 100 GiB = $8/month)." : ""}"
  default      = tostring(local.workspace_storage.default)
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 26

  validation {
    min = local.workspace_storage.min
    max = local.workspace_storage.max
  }
}

data "coder_parameter" "disk_throughput_mbps" {
  count = local.is_ebs_storage ? 1 : 0

  name         = "disk_throughput_mbps"
  display_name = "Disk sequential throughput (MB/s)"
  description  = "Provisioned sequential throughput.\n\nExtra monthly cost: $0.04 per MB/s above 125 MB/s (e.g. 500 MB/s = +$15/month)."
  default      = "500"
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 27

  validation {
    min = 125
    max = 1000
  }
}

data "coder_parameter" "disk_iops" {
  count = local.is_ebs_storage ? 1 : 0

  name         = "disk_iops"
  display_name = "Disk random 4K IOPS"
  description  = "Provisioned random 4K IOPS.\n\nExtra monthly cost: $0.005 per IOPS above 3,000 (e.g. 8,000 IOPS = +$25/month)."
  default      = "8000"
  type         = "number"
  form_type    = "slider"
  mutable      = true
  order        = 28

  validation {
    min = 3000
    max = 16000
  }
}

# Coder previews parameters from the template import plan and cannot refresh
# provider-backed options. The authenticated catalog supplies this opaque value;
# the real workspace plan validates it against the owner-scoped snapshot index.
data "coder_parameter" "restore_selector" {
  name         = "restore_selector"
  display_name = "Restore from backup"
  description  = "Open the [verified backup catalog](https://coder-snapshots.${var.access_alias_domain}) to copy a backup selector, or leave this field blank to start without restoring."
  default      = ""
  ephemeral    = true
  form_type    = "input"
  mutable      = true
  order        = 29
  styling      = jsonencode({ placeholder = "Verified backup selector" })

  validation {
    regex = "^$|^${local.start_fresh_selector}$|${local.snapshot_selector_pattern}"
    error = "Use a selector supplied by the verified backup catalog, or leave this field blank."
  }
}

data "coder_parameter" "ssh_enabled" {
  name         = "ssh_enabled"
  display_name = "SSH access"
  description  = "Enable ordinary OpenSSH. Paseo and browser VS Code do not require it."
  default      = "false"
  type         = "bool"
  form_type    = "checkbox"
  mutable      = true
  order        = 30
}

data "coder_parameter" "ssh_public_key" {
  name         = "ssh_public_key"
  display_name = "New SSH public key (optional)"
  description  = <<-EOT
    **macOS setup: run this once, then paste the copied public key below:**

    ```sh
    key=~/.ssh/id_ed25519; mkdir -p ~/.ssh && { [ -f "$key" ] || ssh-keygen -t ed25519 -f "$key"; } && pbcopy < "$key.pub"
    ```

    The command reuses your default Ed25519 key, or creates one, so `ssh`
    offers it without any `~/.ssh/config` entry. The private key stays on your Mac. A non-empty value replaces the saved
    public key. Disabling SSH keeps the key for later use.
  EOT
  default      = ""
  mutable      = true
  order        = 31
  styling      = jsonencode({ disabled = data.coder_parameter.ssh_enabled.value != "true" })

  validation {
    regex = data.coder_parameter.ssh_enabled.value == "true" ? "^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-P][A-Za-z0-9+/]{42}( [^\\x00-\\x1F\\x7F]+)?$" : "^$|^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-P][A-Za-z0-9+/]{42}( [^\\x00-\\x1F\\x7F]+)?$"
    error = "Enable SSH with one structurally valid OpenSSH Ed25519 public key."
  }
}
