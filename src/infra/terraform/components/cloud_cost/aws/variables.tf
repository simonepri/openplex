# Declares input variables for AWS cost report bucket prefixes, retention, and encryption configurations.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL of the cluster."
  type        = string
}

variable "cluster_oidc_arn" {
  description = "ARN of the cluster OIDC provider in AWS IAM."
  type        = string
}

variable "aws_region" {
  description = "AWS region for billing export bucket and CUR report."
  type        = string
  default     = "us-west-2"
}
