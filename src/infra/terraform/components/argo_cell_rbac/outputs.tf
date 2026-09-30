# Exports the ServiceAccount name, namespace, and authentication token for cell registration.

output "service_account_name" {
  description = "Name of the configured Argo CD manager ServiceAccount."
  value       = kubernetes_service_account_v1.argocd_manager.metadata[0].name
}

output "namespace" {
  description = "Namespace where the Argo CD manager ServiceAccount resides."
  value       = var.namespace
}

output "secret_name" {
  description = "Name of the secret containing the ServiceAccount bearer token."
  value       = kubernetes_secret_v1.argocd_manager_token.metadata[0].name
}

output "token" {
  description = "Bearer token for the Argo CD manager ServiceAccount."
  value       = try(kubernetes_secret_v1.argocd_manager_token.data["token"], null)
  sensitive   = true
}
