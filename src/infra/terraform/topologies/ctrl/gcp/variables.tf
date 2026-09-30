# Declares input variables for GCP control plane deployment including VPC CIDRs, DNS, and GKE configs.

variable "cluster_name" {
  description = "Name of the control plane cluster."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the control plane VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones for subnet allocation."
  type        = list(string)
}

variable "tier_subnets" {
  description = "Subnet CIDR allocations by network tier."
  type = object({
    private = list(string)
    public  = list(string)
    pod     = list(string)
  })
}

variable "kubernetes_version" {
  description = "Kubernetes control plane version."
  type        = string
  default     = "1.36"
}

variable "enable_control_plane_logging" {
  description = "Whether to enable control plane logging for the Kubernetes cluster."
  type        = bool
  default     = true
}

variable "enable_identity" {
  description = "Whether to provision cloud identity / IAM roles."
  type        = bool
  default     = true
}

variable "enable_dns" {
  description = "Whether to provision DNS zones."
  type        = bool
  default     = true
}

variable "enable_network_mesh" {
  description = "Whether to provision the Tailscale network mesh router."
  type        = bool
  default     = true
}

variable "enable_cloud_cost" {
  description = "Whether to provision cloud billing export infrastructure and OpenCost IAM."
  type        = bool
  default     = false
}

variable "enable_kms_secrets_encryption" {
  description = "Whether to enable KMS envelope encryption for Kubernetes secrets."
  type        = bool
  default     = true
}

variable "enable_flow_logs" {
  description = "Whether to enable VPC flow logs."
  type        = bool
  default     = true
}


variable "domain_name" {
  description = "Internal DNS domain name for the control plane."
  type        = string
  default     = "corp.local.internal"
}

variable "intranet_domain_name" {
  description = "Intranet domain name for internal routing and mesh services (e.g. corp.local.internal)."
  type        = string
  default     = "corp.local.internal"
}

variable "public_domain_name" {
  description = "Public domain name for the fleet identity and user email accounts (e.g. local.internal)."
  type        = string
  # nosemgrep: repository.lint.forbidden-domain-patterns
  default = "local.internal"
}

variable "cluster_domain_name" {
  description = "DNS suffix for cluster access and routing (e.g. c.corp.local.internal)."
  type        = string
  default     = null
}

variable "access_domain_name" {
  description = "Deprecated: Use cluster_domain_name instead."
  type        = string
  default     = null
}

variable "oidc_tls_insecure_skip_verify" {
  description = "Whether Argo CD skips verification of its OIDC provider certificate."
  type        = bool
  default     = false
}

variable "tailnet_auth_key" {
  description = "Tailscale auth key for the mesh router."
  type        = string
  sensitive   = true
  default     = ""
}

variable "git_repo_url" {
  description = "Git repository URL containing the fleet manifests."
  type        = string
}

variable "target_revision" {
  description = "Git revision for Argo CD fleet tracking."
  type        = string
  default     = "HEAD"
}

variable "resource_prefix" {
  description = "Global unbranded resource prefix for cloud resources."
  type        = string
  default     = ""
}

variable "registered_cells" {
  description = "List of cell clusters to register with the control plane Argo CD."
  type = list(object({
    name           = string
    endpoint       = string
    ca_certificate = string
    provider       = optional(string)
    environment    = optional(string)
    profile        = optional(string)
    token          = optional(string)
    annotations    = optional(map(string), {})
    labels         = optional(map(string), {})
    aws_auth_config = optional(object({
      cluster_name = string
      role_arn     = string
    }))
    exec_provider_config = optional(object({
      command     = string
      args        = list(string)
      api_version = string
    }))
  }))
  default = []
}

variable "annotations" {
  description = "Optional annotations to add to the control plane cluster registration secret."
  type        = map(string)
  default     = {}
}

variable "fleet_availability" {
  description = "Availability profile selected by the root fleet Application (standalone, replicated, or resilient)."
  type        = string
  default     = "resilient"

  validation {
    condition     = contains(["replicated", "resilient", "standalone"], var.fleet_availability)
    error_message = "fleet_availability must be standalone, replicated, or resilient."
  }
}
