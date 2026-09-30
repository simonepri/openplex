# Declares input variables configuring Coder agent associations, snapshot schedules, and S3 repository bindings.

variable "agent_id" {
  description = "Coder agent ID to attach snapshot and restore scripts to."
  type        = string

  validation {
    condition     = length(trimspace(var.agent_id)) > 0
    error_message = "agent_id must not be empty."
  }
}

variable "app_labels" {
  description = "Labels for Kubernetes resources."
  type        = map(string)
  default     = {}
}

variable "home_disk_gib" {
  description = "Home disk size in GiB."
  type        = number

  validation {
    condition     = var.home_disk_gib > 0
    error_message = "home_disk_gib must be greater than 0."
  }
}

variable "kopia_repository_bucket" {
  description = "S3 bucket name for snapshot repository and manifests."
  type        = string
  default     = ""
}

variable "lineage_input" {
  description = "Optional canonical lineage input or rewind token. Defaults to workspace_id."
  type        = string
  default     = ""
}

variable "max_bandwidth_mbps" {
  description = "Client-side bandwidth limit in Mbps for Kopia (0 = unlimited)."
  type        = number
  default     = 0

  validation {
    condition     = var.max_bandwidth_mbps >= 0
    error_message = "max_bandwidth_mbps must be non-negative."
  }
}

variable "owner_id" {
  description = "Coder owner ID."
  type        = string

  validation {
    condition     = length(trimspace(var.owner_id)) > 0
    error_message = "owner_id must not be empty."
  }
}

# tflint-ignore: terraform_unused_declarations
variable "owner_username" {
  description = "Coder owner username."
  type        = string

  validation {
    condition     = length(trimspace(var.owner_username)) > 0
    error_message = "owner_username must not be empty."
  }
}

variable "restore_selector" {
  description = "Selected snapshot ID to restore (or empty)."
  type        = string
  default     = ""
}

variable "s3_endpoint" {
  description = "Optional custom S3 endpoint URL (e.g. for in-cluster S3 gateway http://s3-gateway.s3-system.svc:8080)."
  type        = string
  default     = ""
}

# tflint-ignore: terraform_unused_declarations
variable "single_writer_guard_enabled" {
  description = "Whether to guard against concurrent writers to the same lineage."
  type        = bool
  default     = true
}

variable "snapshot_interval" {
  description = "Cron expression for periodic snapshot."
  type        = string
  default     = "0 */30 * * * *"
}

variable "storage_class_name" {
  description = "Storage class for the home PVC."
  type        = string
  default     = null
}

# tflint-ignore: terraform_unused_declarations
variable "team" {
  description = "Team name."
  type        = string

  validation {
    condition     = length(trimspace(var.team)) > 0
    error_message = "team must not be empty."
  }
}

variable "workspace_id" {
  description = "ID of the Coder workspace."
  type        = string

  validation {
    condition     = length(trimspace(var.workspace_id)) > 0
    error_message = "workspace_id must not be empty."
  }
}

variable "workspace_name" {
  description = "Name of the Coder workspace."
  type        = string

  validation {
    condition     = length(trimspace(var.workspace_name)) > 0
    error_message = "workspace_name must not be empty."
  }
}

variable "target_dir" {
  description = "Target directory to back up and restore."
  type        = string
  default     = ""
}

variable "workspace_namespace" {
  description = "Kubernetes namespace."
  type        = string

  validation {
    condition     = length(trimspace(var.workspace_namespace)) > 0
    error_message = "workspace_namespace must not be empty."
  }
}
