# Declares input variables for production domain names, region allocations, and cloud provider credentials.

variable "aws_region" {
  description = "AWS region for the control plane and primary cell."
  type        = string
  default     = "us-west-2"
}

variable "gcp_project" {
  description = "GCP project ID for multi-cloud cell."
  type        = string
  default     = null
}

variable "gcp_region" {
  description = "GCP region for secondary cell."
  type        = string
  default     = "europe-west4"
}

variable "git_repo_url" {
  description = "Git repository URL containing the fleet manifests; defaults to git.url in deployment.yaml."
  type        = string
  default     = null
}

variable "public_domain" {
  description = "Public domain name for the production deployment; defaults to public_domain in deployment.yaml."
  type        = string
  default     = null
}

variable "resource_prefix" {
  description = "Global unbranded resource prefix for naming cloud resources."
  type        = string
  default     = "corp"
}

variable "system_instance_types" {
  description = "Instance types for the EKS system managed node group."
  type        = list(string)
  default     = ["m5.2xlarge"]
}

variable "target_revision" {
  description = "Git revision for Argo CD fleet tracking."
  type        = string
  default     = "HEAD"
}

variable "tailnet_auth_key" {
  description = "Tailscale auth key for mesh routing."
  type        = string
  sensitive   = true
  default     = ""
}

variable "enable_gcp_cell" {
  description = "Whether to provision the GCP secondary cell cluster."
  type        = bool
  default     = false
}

variable "enable_cloud_cost" {
  description = "Whether to provision cloud billing export infrastructure and OpenCost IAM (requires AWS Organization Payer account)."
  type        = bool
  default     = false
}

variable "tailscale_api_key" {
  description = "Tailscale API key for tailscale provider coordination."
  type        = string
  sensitive   = true
  default     = ""
}

variable "tailnet_name" {
  description = "Tailscale tailnet name."
  type        = string
  default     = ""
}

variable "manage_tailscale_acl" {
  description = "Whether to manage and overwrite the root Tailscale ACL via OpenTofu (default false to prevent accidental overwrite of existing Tailnets)."
  type        = bool
  default     = false
}
