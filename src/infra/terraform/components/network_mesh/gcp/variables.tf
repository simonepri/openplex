# Declares input variables for GCP Tailscale router VPC subnets, machine types, and advertised routes.

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
