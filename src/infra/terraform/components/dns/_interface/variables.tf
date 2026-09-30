# Declares provider-neutral input variables for managed DNS domain names and parent zone delegation.

variable "domain_name" {
  description = "Public or cell DNS domain name without a trailing dot."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)+$", var.domain_name))
    error_message = "domain_name must be a valid lowercase DNS domain name without a trailing dot."
  }
}

variable "is_cell" {
  description = "Whether this DNS zone is for a cell delegated from a parent zone."
  type        = bool
  default     = false
}

variable "parent_zone_id" {
  description = "Parent hosted zone ID to attach the NS delegation record to when is_cell is true."
  type        = string
  default     = ""
}

variable "realized" {
  description = "Realized cloud resources passed from the root module to shape the canonical record."
  type        = any
  default     = null
}
