# Declares input variables for AWS control plane deployment including VPC CIDRs, DNS, and EKS configs.

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

variable "service_ipv4_cidr" {
  description = "CIDR block to assign Kubernetes service IP addresses."
  type        = string
  default     = null
}

variable "enable_control_plane_logging" {
  description = "Whether to enable control plane logging for the Kubernetes cluster."
  type        = bool
  default     = true
}

variable "enable_access_config" {
  description = "Whether to configure the native EKS access block."
  type        = bool
  default     = true
}

variable "enable_addons" {
  description = "Whether to install native EKS managed add-ons."
  type        = bool
  default     = true
}

variable "enable_ebs_csi" {
  description = "Whether to install the AWS EBS CSI driver add-on (set false for Floci emulator)."
  type        = bool
  default     = true
}

variable "public_access_cidrs" {
  description = "List of CIDR blocks allowed to access the public EKS API endpoint."
  type        = list(string)
  default     = []
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

variable "enable_tailscale_operator" {
  description = "Whether to provision Tailscale Kubernetes operator OAuth credentials."
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

variable "cluster_provider" {
  description = "Provider label exposed to GitOps cluster selectors."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "floci"], var.cluster_provider)
    error_message = "cluster_provider must be aws or floci."
  }
}

variable "system_instance_types" {
  description = "Instance types for the control plane system managed node group."
  type        = list(string)
  default     = ["m5.2xlarge"]
}

variable "resource_prefix" {
  description = "Global unbranded resource prefix for cloud resources."
  type        = string
  default     = ""
}

variable "cluster_environment" {
  description = "Environment label exposed to GitOps cluster selectors."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["local", "production"], var.cluster_environment)
    error_message = "cluster_environment must be local or production."
  }
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

variable "manage_tailscale_acl" {
  description = "Whether to manage and overwrite the root Tailscale ACL via OpenTofu (requires tailscale provider and API key)."
  type        = bool
  default     = false
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

variable "disabled_components" {
  description = "Set of standard component names to disable in this topology."
  type        = set(string)
  default     = []
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

variable "system_desired_size" {
  description = "Desired number of worker nodes in the system node group."
  type        = number
  default     = 2
}

variable "system_max_size" {
  description = "Maximum number of worker nodes in the system node group."
  type        = number
  default     = 3
}

variable "system_node_taints" {
  description = "List of taints to apply to the system node group."
  type = list(object({
    key    = string
    value  = string
    effect = string
  }))
  default = [
    {
      key    = "CriticalAddonsOnly"
      value  = "true"
      effect = "NO_SCHEDULE"
    }
  ]
}

variable "cluster_labels" {
  description = "Optional labels to add to the control plane cluster registration secret."
  type        = map(string)
  default     = {}
}

variable "node_repair_enabled" {
  description = "Whether to enable node auto repair for the system managed node group."
  type        = bool
  default     = true
}



variable "shared_secret_names" {
  description = "Account-wide secret store entries, outside this cluster's name prefix, that its external-secrets controller may read."
  type        = list(string)
  default     = []
}

variable "profiles_retention_days" {
  description = "Number of days to retain Parca profiles before expiration."
  type        = number
  default     = 30
}


variable "publisher_oidc_repository" {
  description = "Repository part of the GitHub Actions OIDC subject the image publisher trusts, when it differs from the owner/name slug (for example owner@id/name@id)."
  type        = string
  default     = null
}
