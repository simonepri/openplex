# Declares input variables for AWS ECR repository names, image scanning, and tag mutability settings.

variable "installation_name" {
  description = "Installation identifier used as the repository namespace."
  type        = string
}

variable "repositories" {
  description = "List of repository names to provision within the registry."
  type        = list(string)
  default     = ["workloads", "workspace", "infrastructure"]
}
