# Declares input variables for the Argo CD cell manager ServiceAccount and RBAC binding.

variable "namespace" {
  description = "Namespace where the Argo CD manager service account and secret reside."
  type        = string
  default     = "kube-system"
}

variable "service_account_name" {
  description = "Name of the Argo CD manager service account."
  type        = string
  default     = "argocd-manager"
}

variable "cluster_role_name" {
  description = "Name of the ClusterRole to bind to the Argo CD manager service account."
  type        = string
  default     = "cluster-admin"
}
