# Declares provider-neutral input variables for IAM role configurations and workload identity mappings.

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

variable "realized" {
  description = "Provider-realized state passed back to the contract."
  type        = any
  default     = null
}

