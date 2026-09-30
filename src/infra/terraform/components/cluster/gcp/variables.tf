# Declares input variables for GCP GKE cluster locations, subnets, and node pool specifications.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "vpc_id" {
  description = "VPC network name or self-link where the cluster is deployed."
  type        = string
}

variable "subnet_ids" {
  description = "Subnet names or self-links for the cluster and nodes."
  type        = list(string)
}

variable "kubernetes_version" {
  description = "Kubernetes control plane version."
  type        = string
  default     = "1.36"
}

variable "enable_kms_secrets_encryption" {
  description = "Whether to enable KMS envelope encryption for Kubernetes secrets."
  type        = bool
  default     = true
}

variable "enable_control_plane_logging" {
  description = "Whether to enable control plane logging for the Kubernetes cluster."
  type        = bool
  default     = true
}

variable "master_ipv4_cidr_block" {
  description = "IPv4 CIDR block reserved for the GKE master control plane."
  type        = string
  default     = "172.17.32.0/28"
}

