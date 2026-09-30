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
