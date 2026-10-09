# Declares input variables for AWS cell topology deployment including VPC CIDRs and EKS configs.

variable "cluster_name" {
  description = "Name of the workload cell cluster."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the cell VPC."
  type        = string
  default     = "10.1.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones for subnet allocation."
  type        = list(string)
  default     = []
}

variable "tier_subnets" {
  description = "Subnet CIDR allocations by network tier."
  type = object({
    private = list(string)
    public  = list(string)
    pod     = list(string)
  })
  default = {
    private = []
    public  = []
    pod     = []
  }
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
  description = "Internal DNS domain name for the cell."
  type        = string
  default     = "corp.local.internal"
}

variable "parent_zone_id" {
  description = "Parent hosted zone ID for DNS delegation."
  type        = string
  default     = ""
}

variable "tailnet_auth_key" {
  description = "Tailscale auth key for the mesh router."
  type        = string
  sensitive   = true
  default     = ""
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

variable "system_instance_types" {
  description = "Instance types for the cell system managed node group."
  type        = list(string)
  default     = ["m5.2xlarge"]
}

variable "iam_name_prefix" {
  description = "Prefix applied to IAM role, policy, and instance profile names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.iam_name_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.iam_name_prefix))
    error_message = "iam_name_prefix must be at most 16 characters and contain only lowercase letters, digits, and hyphens."
  }
}

variable "kms_alias_prefix" {
  description = "Prefix applied to KMS key alias names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.kms_alias_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.kms_alias_prefix))
    error_message = "kms_alias_prefix must be at most 16 characters and contain only lowercase letters, digits, and hyphens."
  }
}

variable "iam_permissions_boundary" {
  description = "ARN of the permissions boundary policy to attach to IAM roles."
  type        = string
  default     = null
}

variable "tags" {
  description = "Custom resource tags applied across cloud resources."
  type        = map(string)
  default     = {}
}

variable "disabled_components" {
  description = "Set of standard component names to disable in this topology."
  type        = set(string)
  default     = []
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

variable "mesh_peer_cidrs" {
  description = "List of peer CIDRs routed through the Tailscale network mesh router."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.mesh_peer_cidrs : can(cidrnetmask(cidr))
    ])
    error_message = "All mesh_peer_cidrs entries must be valid IPv4 CIDR blocks."
  }
}

variable "global_storage" {
  description = "Global storage configuration containing endpoint and per-team credentials."
  type = object({
    endpoint = string
    teams = map(object({
      bucket            = string
      access_key_id     = string
      secret_access_key = string
    }))
    readers = map(object({
      bucket            = string
      access_key_id     = string
      secret_access_key = string
    }))
  })
  sensitive = true
  default   = null
}

variable "atlantis_plan_role_arn" {
  description = "IAM role ARN assumed by Atlantis during plan operations for EKS access."
  type        = string
  default     = ""
}

variable "atlantis_apply_role_arn" {
  description = "IAM role ARN assumed by Atlantis during apply operations for EKS access."
  type        = string
  default     = ""
}
