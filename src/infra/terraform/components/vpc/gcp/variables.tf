# Declares input variables for GCP VPC networks, subnetwork IP ranges, and Cloud NAT configs.

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

variable "service_cidr" {
  description = "CIDR block to assign Kubernetes service IP addresses."
  type        = string
  default     = "10.96.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.service_cidr))
    error_message = "service_cidr must be a valid IPv4 CIDR block."
  }
}

