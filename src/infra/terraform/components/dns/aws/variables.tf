# Declares input variables for Route 53 DNS domain names, zone delegation, and tag configurations.

variable "domain_name" {
  description = "Public or cell DNS domain name without a trailing dot."
  type        = string
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
