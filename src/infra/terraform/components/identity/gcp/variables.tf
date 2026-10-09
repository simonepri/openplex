# Declares input variables for Google service account names, project IAM roles, and Kubernetes bindings.

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

