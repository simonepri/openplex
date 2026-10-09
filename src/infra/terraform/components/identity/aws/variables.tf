# Declares input variables for IAM role configurations, cluster OIDC issuers, and service account trusts.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL of the cluster."
  type        = string
}

variable "cluster_oidc_arn" {
  description = "ARN of the cluster OIDC provider in AWS IAM."
  type        = string
  default     = ""
}

variable "project_id" {
  description = "GCP project ID hosting the workload identity pool."
  type        = string
  default     = ""
}

variable "roles" {
  description = "Workload identities to project into cloud IAM, keyed by role identifier."
  type = map(object({
    namespace       = string
    service_account = string
  }))
}

variable "trust_mode" {
  description = "Workload identity trust mechanism: 'pod_identity' (native EKS) or 'federated' (cross-cloud OIDC)."
  type        = string
  default     = "pod_identity"

  validation {
    condition     = contains(["pod_identity", "federated"], var.trust_mode)
    error_message = "trust_mode must be either 'pod_identity' or 'federated'."
  }
}

variable "oidc_thumbprints" {
  description = "List of root certificate thumbprints for the federated OIDC provider."
  type        = list(string)
  default = [
    # Google Trust Services root CA (for GKE clusters)
    "0874fb5c3d54251267366ce2d07599761ad74aa3",
    # Standard AWS OIDC fallback thumbprints
    "9e99a48a9960b14926cc7f3b02372d874a309192",
    "9687e8340d027dc7eb98b965c7dae9e73f443b7e",
  ]
}


variable "shared_secret_names" {
  description = "Account-wide Secrets Manager entries, outside the cluster's name prefix, that the external-secrets role may read."
  type        = list(string)
  default     = []
}

variable "storage_kms_key_arn" {
  description = "ARN of the KMS customer-managed key used for storage encryption."
  type        = string
  default     = ""
}

variable "storage_meta_bucket_arn" {
  description = "ARN of the meta storage bucket holding S3 inventory reports."
  type        = string
  default     = ""
}

variable "storage_stats_inventory_reports" {
  description = "Inventory report buckets and prefixes readable by the ClickHouse rollup role."
  type = list(object({
    bucket_arn = string
    prefixes   = list(string)
  }))
  default = []
}

variable "opentofu_state_bucket" {
  description = "Name of the S3 bucket hosting OpenTofu remote state accessed by Atlantis."
  type        = string
  default     = ""
}

variable "iam_name_prefix" {
  description = "Prefix applied to IAM role names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.iam_name_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.iam_name_prefix))
    error_message = "iam_name_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "iam_permissions_boundary" {
  description = "ARN of the permissions boundary policy to attach to IAM roles."
  type        = string
  default     = null
}
