# Declares input variables for AWS Secrets Manager secret names, descriptions, and KMS key configurations.

variable "secret_name" {
  description = "Name of the secret."
  type        = string
}

variable "secret_values" {
  description = "Key-value pairs stored in the secret."
  type        = map(string)
  sensitive   = true
}

variable "recovery_window_in_days" {
  description = "Number of days that AWS Secrets Manager waits before deleting the secret. Can be 0 to force immediate deletion."
  type        = number
  default     = 0
}
