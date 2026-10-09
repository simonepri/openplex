# Declares input variables for AWS Tailscale router VPC subnets, instance types, and subnet routes.

variable "name" {
  description = "Name identifier for the network mesh router."
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC where the mesh router is deployed."
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID where the mesh router instance is placed."
  type        = string
}

variable "tailnet_auth_key" {
  description = "Tailscale auth key used to authenticate the mesh router into the tailnet."
  type        = string
  sensitive   = true
}

variable "advertised_routes" {
  description = "CIDR routes advertised by this mesh router."
  type        = list(string)
  default     = []
}

variable "instance_type" {
  description = "EC2 instance type for the network mesh router."
  type        = string
  default     = "t4g.small"
}

variable "enable_k8s_operator" {
  description = "Whether to provision Tailscale Kubernetes operator OAuth credentials in Secrets Manager."
  type        = bool
  default     = false
}

variable "operator_tags" {
  description = "Tags assigned to devices created by the Kubernetes operator."
  type        = list(string)
  default     = ["tag:k8s-operator"]
}

variable "clamp_tunnel_mss" {
  description = "Whether to clamp TCP MSS on connections forwarded through the tailnet so packets fit the tunnel MTU."
  type        = bool
  default     = false
}

variable "masquerade_tunnel_egress" {
  description = "Whether to rewrite the source of VPC traffic sent into the tailnet to the router's tailnet address, so peers that do not accept this router's subnet routes still answer."
  type        = bool
  default     = false
}

variable "cluster_name" {
  description = "Cluster name for resource naming and KMS aliases."
  type        = string
}

variable "iam_name_prefix" {
  description = "Prefix applied to IAM role, policy, user, and instance profile names."
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
  description = "ARN of the permissions boundary policy to attach to IAM roles."
  type        = string
  default     = null
}
