# Declares input variables for AWS cost report bucket prefixes, retention, and encryption configurations.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "account_id" {
  description = "AWS account ID for globally unique bucket naming."
  type        = string
}

variable "iam_name_prefix" {
  description = "Prefix for IAM resource names."
  type        = string
  default     = ""

  validation {
    condition     = length(var.iam_name_prefix) <= 16 && can(regex("^[a-z0-9-]*$", var.iam_name_prefix))
    error_message = "The iam_name_prefix must be at most 16 characters and contain only lowercase letters, digits, and hyphens."
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
  description = "ARN of the permissions boundary managed policy to attach to IAM roles."
  type        = string
  default     = null
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
