# Declares input variables for AWS EKS cluster versions, VPC subnets, and node group configurations.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "vpc_id" {
  description = "VPC ID where the cluster is deployed."
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for the cluster control plane and worker nodes."
  type        = list(string)
}

variable "kubernetes_version" {
  description = "Kubernetes control plane version."
  type        = string
  default     = "1.36"
}

variable "service_ipv4_cidr" {
  description = "CIDR block to assign Kubernetes service IP addresses."
  type        = string
  default     = null
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

variable "enable_access_config" {
  description = "Whether to configure the native EKS access block."
  type        = bool
  default     = true
}

variable "public_access_cidrs" {
  description = "List of CIDR blocks allowed to access the public EKS API endpoint."
  type        = list(string)
  default     = []
}

variable "enable_network_policy" {
  description = "Whether to enable VPC CNI NetworkPolicy support on the cluster."
  type        = bool
  default     = true
}

variable "enable_addons" {
  description = "Whether to install native EKS managed add-ons."
  type        = bool
  default     = true
}

variable "enable_ebs_csi" {
  description = "Whether to install the AWS EBS CSI driver add-on (set false for Floci emulator)."
  type        = bool
  default     = true
}

variable "system_instance_types" {
  description = "Instance types for the EKS system managed node group."
  type        = list(string)
  default     = ["m5.2xlarge"]
}
