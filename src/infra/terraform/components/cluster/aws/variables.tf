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

variable "system_desired_size" {
  description = "Desired number of worker nodes in the system node group."
  type        = number
  default     = 2
}

variable "system_max_size" {
  description = "Maximum number of worker nodes in the system node group."
  type        = number
  default     = 4
}

variable "node_repair_enabled" {
  description = "Whether to enable node auto repair for the system managed node group."
  type        = bool
  default     = true
}

variable "pod_subnet_ids" {
  description = "Dedicated pod subnets, one per availability zone, for VPC CNI custom networking with prefix delegation; empty keeps pods on node subnets."
  type        = list(string)
  default     = []
}

variable "system_max_pods" {
  description = "Kubelet maxPods for system nodes when pod subnets enable prefix delegation (EKS recommends 110 below 30 vCPUs)."
  type        = number
  default     = 110
}

variable "system_node_taints" {
  description = "Taints to apply to the system node group nodes."
  type = list(object({
    key    = string
    value  = string
    effect = string
  }))
  default = []
}

variable "enable_karpenter_interruption" {
  description = "Whether to enable Karpenter interruption handling via SQS and EventBridge."
  type        = bool
  default     = true
}

variable "karpenter_interruption_retention_seconds" {
  description = "Message retention in seconds for the Karpenter interruption SQS queue."
  type        = number
  default     = 300
}

variable "iam_name_prefix" {
  description = "Prefix applied to IAM role and instance profile names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.iam_name_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.iam_name_prefix))
    error_message = "iam_name_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "kms_alias_prefix" {
  description = "Prefix applied to KMS key alias names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.kms_alias_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.kms_alias_prefix))
    error_message = "kms_alias_prefix must be at most 16 characters and contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "iam_permissions_boundary" {
  description = "ARN of the permissions boundary policy applied to IAM roles."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to cluster resources and controller add-ons."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.tags) : can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", k))])
    error_message = "All tag keys must be lowercase kebab-case."
  }
}

variable "atlantis_plan_role_arn" {
  description = "IAM role ARN assumed by Atlantis during plan operations for EKS access."
  type        = string
  default     = ""
}

variable "atlantis_apply_role_arn" {
  description = "IAM role ARN assumed by Atlantis during apply operations for EKS access."
  type        = string
  default     = ""
}
