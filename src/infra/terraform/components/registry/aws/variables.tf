# Declares input variables for AWS ECR repository names, image scanning, and tag mutability settings.

variable "cluster_name" {
  description = "Name of the owning cluster."
  type        = string
}

variable "repositories" {
  description = "List of repository names to provision within the registry."
  type        = list(string)
  default     = ["workloads", "workspace", "infrastructure"]
}

variable "kms_alias_prefix" {
  description = "Prefix applied to KMS key alias names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.kms_alias_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.kms_alias_prefix))
    error_message = "kms_alias_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}
