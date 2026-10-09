# Declares input variables for S3 bucket prefixes, versioning rules, and encryption settings.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "account_id" {
  description = "AWS account ID for globally unique bucket naming."
  type        = string
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

variable "storage_tiers" {
  description = "List of storage tiers to provision."
  type        = list(string)
  default     = ["home", "scratch", "archive", "backups", "meta", "logs"]
}

variable "profiles_retention_days" {
  description = "Number of days before Parca profiles expire in object storage."
  type        = number
  default     = 30
}
