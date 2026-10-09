# Declares input variables for the Cloudflare R2 storage component.

variable "account_id" {
  description = "Cloudflare account ID managing the R2 storage buckets and tokens."
  type        = string
}

variable "name_prefix" {
  description = "Owning control plane cluster name used as prefix for R2 bucket names."
  type        = string
}

variable "account_suffix" {
  description = "AWS account ID used as suffix for globally shared R2 bucket names."
  type        = string
}

variable "location" {
  description = "Location hint for R2 bucket creation (e.g. wnam)."
  type        = string
  default     = "wnam"
}

variable "teams" {
  description = "Set of team identifiers for which to provision R2 storage."
  type        = set(string)
}
