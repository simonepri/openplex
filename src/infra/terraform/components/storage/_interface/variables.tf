# Declares provider-neutral input variables for storage bucket tiers, retention, and lifecycle policies.

variable "installation_name" {
  description = "Installation identifier for resource naming."
  type        = string
}

variable "cell_name" {
  description = "Cell identifier for resource naming."
  type        = string
}

variable "storage_tiers" {
  description = "List of storage tiers to provision."
  type        = list(string)
  default     = ["home", "scratch", "archive", "backups", "meta", "logs"]
}

variable "realized" {
  description = "Realized cloud resources passed from the root module to shape the canonical record."
  type        = any
  default     = null
}
