# Declares input variables for local cluster endpoints, Kubernetes context names, and component toggles.

variable "floci_endpoint" {
  type        = string
  description = "Local Floci AWS emulator endpoint"
  default     = "http://127.0.0.1:4566"
}

variable "git_repo_url" {
  type        = string
  description = "Git repository for Argo CD sync"
  default     = "git://172.19.255.21:9418/openplex.git"
}

variable "git_identity_url" {
  type        = string
  description = "Local smart-HTTP repository URL used by Argo reference discovery and webhook matching."
  # LINT.IfChange(webhook-repository)
  default = "http://172.19.255.21:9419/cgi-bin/git/openplex.git"
  # LINT.ThenChange(//src/infra/tools/cloud_emulator/stack/compose.yaml:webhook-repository)
}

variable "fleet_availability" {
  type        = string
  description = "Fleet availability profile (standalone, replicated, or resilient)"
  default     = "standalone"
}

variable "iam_name_prefix" {
  description = "Prefix prepended verbatim before the cluster name of every IAM role, policy, user, and instance profile."
  type        = string
  default     = ""

  validation {
    condition     = length(var.iam_name_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.iam_name_prefix))
    error_message = "iam_name_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "kms_alias_prefix" {
  description = "Prefix prepended verbatim before the cluster name of every KMS alias."
  type        = string
  default     = ""

  validation {
    condition     = length(var.kms_alias_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.kms_alias_prefix))
    error_message = "kms_alias_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "iam_permissions_boundary" {
  description = "Managed policy ARN attached as permissions boundary to every created IAM role."
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Prefix prepended to cluster names when sharing a cloud account."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[a-z0-9-]*$", var.name_prefix))
    error_message = "name_prefix must contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "tags" {
  description = "Default tags applied to cloud resources and passed to runtime controllers."
  type        = map(string)
  default = {
    cost-center = "shared-infrastructure"
    environment = "local"
    managed-by  = "opentofu"
  }

  validation {
    condition     = alltrue([for k in keys(var.tags) : can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", k))])
    error_message = "All tag keys must be lowercase kebab-case."
  }
}
