# Declares input variables and normalization parameters for the cloud cost component interface.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL of the cluster."
  type        = string
  default     = ""
}

variable "cluster_oidc_arn" {
  description = "ARN of the cluster OIDC provider in AWS IAM."
  type        = string
  default     = ""
}

variable "project_id" {
  description = "GCP project ID hosting the billing export dataset."
  type        = string
  default     = ""
}

variable "realized" {
  description = "Provider-realized state passed back to the contract."
  type        = any
  default     = null
}
