# Declares input variables for AWS CloudTrail audit logging configuration and event monitoring.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "s3_data_event_bucket_arns" {
  description = "Exact bucket ARNs to monitor data events for."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Resource tags to apply to all provisioned resources."
  type        = map(string)
  default     = {}
}

variable "retention_in_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 90
}
