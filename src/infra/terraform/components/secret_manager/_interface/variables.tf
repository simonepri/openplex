# Declares provider-neutral input variables for secret names, descriptions, and initial secret payloads.

variable "secret_name" {
  description = "Name of the secret."
  type        = string

  validation {
    condition     = length(trimspace(var.secret_name)) > 0
    error_message = "secret_name must not be empty."
  }
}

variable "secret_values" {
  description = "Key-value pairs stored in the secret."
  type        = map(string)
  sensitive   = true
}

variable "realized" {
  description = "Provider resource attributes used to build the canonical record."
  type        = any
  default     = null
}
