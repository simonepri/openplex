# Provisions the ServiceAccount, ClusterRoleBinding, and authentication token for Argo CD cell management.

resource "kubernetes_service_account_v1" "argocd_manager" {
  metadata {
    name      = var.service_account_name
    namespace = var.namespace
  }
}

resource "kubernetes_cluster_role_binding_v1" "argocd_manager" {
  metadata {
    name = "${var.service_account_name}-binding"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = var.cluster_role_name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argocd_manager.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_secret_v1" "argocd_manager_token" {
  metadata {
    name      = "${var.service_account_name}-token"
    namespace = var.namespace
    annotations = {
      "kubernetes.io/service-account.name" = kubernetes_service_account_v1.argocd_manager.metadata[0].name
    }
  }

  type = "kubernetes.io/service-account-token"
}
