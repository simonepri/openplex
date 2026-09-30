# Declares provider-neutral input variables for Tailscale subnet router instances and route advertisements.

variable "name" {
  description = "Name identifier for the network mesh router."
  type        = string

  validation {
    condition     = length(var.name) > 0
    error_message = "name must not be empty."
  }
}

variable "vpc_id" {
  description = "ID of the VPC where the mesh router is deployed."
  type        = string

  validation {
    condition     = length(var.vpc_id) > 0
    error_message = "vpc_id must not be empty."
  }
}

variable "subnet_id" {
  description = "Subnet ID where the mesh router instance is placed."
  type        = string

  validation {
    condition     = length(var.subnet_id) > 0
    error_message = "subnet_id must not be empty."
  }
}

variable "tailnet_auth_key" {
  description = "Tailscale auth key used to authenticate the mesh router into the tailnet."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.tailnet_auth_key) > 0
    error_message = "tailnet_auth_key must not be empty."
  }
}

variable "advertised_routes" {
  description = "CIDR routes advertised by this mesh router."
  type        = list(string)
  default     = []
}

variable "realized" {
  description = "Realized resource outputs passed from the provider root module."
  type        = any
  default     = null
}
