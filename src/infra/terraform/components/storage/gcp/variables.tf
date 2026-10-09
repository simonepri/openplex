# Declares input variables for GCP storage bucket names, locations, and lifecycle rules.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "account_id" {
  description = "GCP project number or ID for globally unique bucket naming."
  type        = string
  default     = ""
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

variable "kms_key_name" {
  description = "Resource ID of a Cloud KMS CryptoKey to encrypt storage tiers with CMEK. If not provided, a dedicated key ring and crypto key are provisioned."
  type        = string
  default     = ""
}

variable "location" {
  description = "GCP location for storage buckets and KMS key ring."
  type        = string
  default     = "US"
}

variable "teams" {
  description = "Set of team slugs for per-team managed folders."
  type        = set(string)
  default     = []
}

variable "team_service_accounts" {
  description = "Map of team slug to Google service account email for folder-scoped IAM bindings."
  type        = map(string)
  default     = {}
}

