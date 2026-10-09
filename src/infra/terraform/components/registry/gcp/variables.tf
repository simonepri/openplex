# Declares input variables for GCP Artifact Registry repository names, formats, and encryption keys.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster used as the repository namespace."
  type        = string
}

variable "repositories" {
  description = "List of repository names to provision within the registry."
  type        = list(string)
  default     = ["workloads", "workspace", "infrastructure"]
}
