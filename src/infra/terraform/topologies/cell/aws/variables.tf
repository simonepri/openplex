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

variable "resource_prefix" {
  description = "Global unbranded resource prefix for cloud resources."
  type        = string
  default     = ""
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
