# Declares provider-neutral input variables for container repository names and image lifecycle rules.

variable "installation_name" {
  description = "Installation identifier used as the repository namespace."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", var.installation_name))
    error_message = "installation_name must be a lowercase alphanumeric hyphen-separated string."
  }
}

variable "repositories" {
  description = "Logical repository names provisioned for the installation."
  type        = list(string)
  default     = ["workloads", "workspace", "infrastructure"]

  validation {
    condition     = alltrue([for r in var.repositories : can(regex("^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", r))])
    error_message = "repositories must contain valid repository names."
  }
}

variable "realized" {
  description = "Realized provider resources used to shape the canonical output record."
  type        = any
  default     = null
}
