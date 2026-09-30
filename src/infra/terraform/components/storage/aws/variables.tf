# Declares input variables for S3 bucket prefixes, versioning rules, and encryption settings.

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

variable "profiles_retention_days" {
  description = "Number of days before Parca profiles expire in object storage."
  type        = number
  default     = 30
}
