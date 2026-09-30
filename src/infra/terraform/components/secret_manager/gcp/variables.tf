# Declares input variables for GCP Secret Manager secret IDs, automatic replication policies, and payloads.

variable "secret_name" {
  description = "Name of the secret."
  type        = string
}

variable "secret_values" {
  description = "Key-value pairs stored in the secret."
  type        = map(string)
  sensitive   = true
}
