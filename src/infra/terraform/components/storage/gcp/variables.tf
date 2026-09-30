# Declares input variables for GCP storage bucket names, locations, and lifecycle rules.

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

variable "kms_key_name" {
  description = "Resource ID of a Cloud KMS CryptoKey to encrypt storage tiers with CMEK. If not provided, a dedicated key ring and crypto key are provisioned."
  type        = string
  default     = ""
}

variable "location" {
  description = "GCP location for storage buckets and KMS key ring."
  type        = string
  default     = "US"
}

