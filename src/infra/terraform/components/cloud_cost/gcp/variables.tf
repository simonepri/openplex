# Declares input variables for GCP BigQuery billing export datasets and OpenCost service accounts.

variable "cluster_name" {
  description = "Name of the Kubernetes cluster."
  type        = string
}

variable "project_id" {
  description = "GCP project ID hosting the billing export dataset."
  type        = string
}

variable "gcp_region" {
  description = "GCP region where the billing export dataset is located."
  type        = string
  default     = "europe-west4"
}
