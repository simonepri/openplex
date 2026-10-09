# Declares input variables for AWS VPC CIDR blocks, availability zones, and subnet allocation masks.

variable "name" {
  description = "Name of the VPC network."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$|^[a-z]$", var.name))
    error_message = "name must be a lowercase DNS-compatible string between 1 and 63 characters."
  }
}

variable "cidr_block" {
  description = "Primary IPv4 CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.cidr_block))
    error_message = "cidr_block must be a valid IPv4 CIDR block."
  }
}

variable "availability_zones" {
  description = "List of availability zones."
  type        = list(string)
  default     = []
}

variable "tier_subnets" {
  description = "Subnet CIDR allocations grouped by tier (private, public, pod)."
  type = object({
    private = optional(list(string), [])
    public  = optional(list(string), [])
    pod     = optional(list(string), [])
  })
  default = {
    private = []
    public  = []
    pod     = []
  }

  validation {
    condition = alltrue([
      for cidr in concat(
        coalesce(var.tier_subnets.private, []),
        coalesce(var.tier_subnets.public, []),
        coalesce(var.tier_subnets.pod, [])
      ) : can(cidrnetmask(cidr))
    ])
    error_message = "All tier subnet CIDRs must be valid IPv4 CIDR blocks."
  }
}

variable "enable_flow_logs" {
  description = "Whether to enable VPC flow logging."
  type        = bool
  default     = true
}

variable "cluster_name" {
  description = "Cluster name for subnet discovery tags."
  type        = string
}

variable "iam_name_prefix" {
  description = "Prefix applied to IAM role, policy, user, and instance profile names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.iam_name_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.iam_name_prefix))
    error_message = "iam_name_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "kms_alias_prefix" {
  description = "Prefix applied to KMS key alias names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.kms_alias_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.kms_alias_prefix))
    error_message = "kms_alias_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "iam_permissions_boundary" {
  description = "ARN of the permissions boundary policy to attach to IAM roles."
  type        = string
  default     = null
}

variable "enable_resolver_query_logging" {
  description = "Whether to enable Route 53 Resolver query logging."
  type        = bool
  default     = true
}

variable "resolver_query_log_retention_days" {
  description = "Retention period in days for Route 53 Resolver query logs."
  type        = number
  default     = 14
}

variable "kms_key_arn" {
  description = "Optional KMS key ARN to encrypt Route 53 Resolver query CloudWatch log group."
  type        = string
  default     = null
}

variable "tags" {
  description = "A map of tags to assign to resources."
  type        = map(string)
  default     = {}
}

