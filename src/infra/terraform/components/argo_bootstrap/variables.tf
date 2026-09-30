# Declares input variables for Argo CD Helm release, repository credentials, and bootstrap applications.

variable "cluster_name" {
  description = "Name of the target Kubernetes cluster."
  type        = string

  validation {
    condition     = length(trimspace(var.cluster_name)) > 0
    error_message = "cluster_name must not be empty."
  }
}

variable "cluster_endpoint" {
  description = "Kubernetes API server endpoint URL."
  type        = string

  validation {
    condition     = length(trimspace(var.cluster_endpoint)) > 0
    error_message = "cluster_endpoint must not be empty."
  }
}

variable "cluster_ca_certificate" {
  description = "Base64-encoded Kubernetes cluster CA certificate."
  type        = string

  validation {
    condition     = length(trimspace(var.cluster_ca_certificate)) > 0
    error_message = "cluster_ca_certificate must not be empty."
  }
}

variable "registered_cells" {
  description = "List of cell clusters to register with Argo CD."
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

variable "domain_name" {
  description = "Domain name for the fleet (e.g. corp.local.internal)."
  type        = string
  default     = "corp.local.internal"
}

variable "intranet_domain_name" {
  description = "Intranet domain name for internal routing and mesh services (e.g. corp.local.internal)."
  type        = string
  default     = "corp.local.internal"
}

variable "public_domain_name" {
  description = "Public domain name for the fleet identity and user email accounts."
  type        = string
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

variable "oidc_issuer" {
  description = "OIDC issuer URL for Argo CD authentication."
  type        = string
  default     = ""
}

variable "oidc_client_secret" {
  description = "OIDC client secret for Argo CD Dex integration."
  type        = string
  default     = "local-only-argocd"
  sensitive   = true
}

variable "oidc_tls_insecure_skip_verify" {
  description = "Whether to skip TLS verification for the OIDC provider (Dex)."
  type        = bool
  default     = false
}

variable "admin_rbac_groups" {
  description = "List of OIDC groups granted admin access to Argo CD."
  type        = list(string)
  default     = ["operators"]
}

variable "git_repo_url" {
  description = "Git fetch transport URL containing fleet manifests; the git-repo-url annotation may override the Argo repository identity."
  type        = string

  validation {
    condition     = length(trimspace(var.git_repo_url)) > 0
    error_message = "git_repo_url must not be empty."
  }
}

variable "upstream_git_repo_url" {
  description = "Upstream Git repository URL redirected to git_repo_url for Git CLI fetches."
  type        = string
  default     = "https://github.com/simonepri/openplex.git"

  validation {
    condition     = length(trimspace(var.upstream_git_repo_url)) > 0
    error_message = "upstream_git_repo_url must not be empty."
  }
}

variable "target_revision" {
  description = "Git branch, tag, or commit to track."
  type        = string
  default     = "HEAD"
}

variable "fleet_availability" {
  description = "Availability profile selected by the root fleet Application."
  type        = string
  default     = "standalone"

  validation {
    condition     = contains(["replicated", "resilient", "standalone"], var.fleet_availability)
    error_message = "fleet_availability must be standalone, replicated, or resilient."
  }
}

variable "cluster_provider" {
  description = "Cloud provider hosting the control plane cluster (e.g. floci, aws, gcp)."
  type        = string
  default     = "floci"
}

variable "cluster_environment" {
  description = "Deployment environment of the control plane cluster (e.g. local, production)."
  type        = string
  default     = "local"
}

variable "annotations" {
  description = "Optional annotations to add to the control plane cluster registration secret."
  type        = map(string)
  default     = {}
}

variable "service_cidr" {
  description = "Kubernetes service CIDR for the control plane cluster."
  type        = string
  default     = ""
}

variable "control_gateway_ipv4" {
  description = "Control gateway IPv4 address for the control plane cluster."
  type        = string
  default     = ""
}

variable "tailscale_oauth_key" {
  description = "Tailscale OAuth client secret or auth key for the control plane cluster."
  type        = string
  default     = ""
  sensitive   = true
}

variable "git_ssh_private_key" {
  description = "Optional SSH private key for Git repository authentication."
  type        = string
  default     = null
  sensitive   = true
}

variable "git_repo_creds_url" {
  description = "URL prefix matching repositories that should use the SSH private key (e.g. git@github.com:my-org)."
  type        = string
  default     = null
}

variable "enable_git_repo_creds" {
  description = "Whether to create an Argo CD repository credential secret for Git SSH access."
  type        = bool
  default     = false
}

variable "cluster_labels" {
  description = "Optional labels to add to the control plane cluster registration secret."
  type        = map(string)
  default     = {}
}


