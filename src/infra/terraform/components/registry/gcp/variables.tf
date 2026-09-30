# Declares input variables for GCP Artifact Registry repository names, formats, and encryption keys.

variable "installation_name" {
  description = "Installation identifier used as the repository namespace."
  type        = string
}

variable "repositories" {
  description = "List of repository names to provision within the registry."
  type        = list(string)
  default     = ["workloads", "workspace", "infrastructure"]
}
