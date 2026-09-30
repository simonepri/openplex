# Installs Argo CD via Helm and provisions seed AppProjects, cluster registration secrets, and root fleet applications.

locals {
  namespace          = "argocd"
  release            = "argocd"
  needs_git_redirect = var.git_repo_url != var.upstream_git_repo_url
  git_identity_url   = try(var.annotations["git-repo-url"], var.git_repo_url)
  cluster_domain     = coalesce(var.cluster_domain_name, var.access_domain_name, "c.${var.intranet_domain_name}")
  registered_cells   = join(",", sort([for cell in var.registered_cells : cell.name]))
}

resource "helm_release" "argocd" {
  name             = local.release
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  namespace        = local.namespace
  create_namespace = true

  set = [
    {
      name  = "server.insecure"
      value = "true"
    }
  ]

  values = concat(
    [
      yamlencode({
        # Argo CD runs on the CriticalAddonsOnly system nodes so it can repair Karpenter-managed capacity.
        global = {
          tolerations = [
            {
              key      = "CriticalAddonsOnly"
              operator = "Equal"
              value    = "true"
              effect   = "NoSchedule"
            }
          ]
        }
        configs = {
          params = {
            "controller.kubectl.parallelism.limit" = "10"
            "controller.operation.processors"      = "5"
            "controller.status.processors"         = "10"
            "reposerver.log.level"                 = "warn"
            "server.insecure"                      = true
          }
          cm = {
            "url" = "https://argocd.${var.domain_name}"
            "additionalUrls" = yamlencode([
              "https://argocd.${local.cluster_domain}",
              "https://argocd.${var.cluster_name}.${local.cluster_domain}",
            ])
            "oidc.tls.insecure.skip.verify"                    = tostring(var.oidc_tls_insecure_skip_verify)
            "timeout.reconciliation"                           = "3600s"
            "timeout.reconciliation.jitter"                    = "600s"
            "resource.customizations.health.ray.io_RayCluster" = local.ray_health.RayCluster
            "resource.customizations.health.ray.io_RayService" = local.ray_health.RayService
            "oidc.config" = yamlencode({
              name            = "Dex"
              issuer          = coalesce(var.oidc_issuer, "https://dex.${var.intranet_domain_name}")
              clientID        = "argocd"
              clientSecret    = "$oidc.dex.clientSecret"
              requestedScopes = ["openid", "profile", "email", "groups"]
            })
            "resource.customizations.ignoreDifferences.resources.signoz.io_AlertRule" = <<-YAML
              jsonPointers:
                - /status
              jqPathExpressions:
                - .status
            YAML
            "resource.customizations.ignoreDifferences.resources.signoz.io_Dashboard" = <<-YAML
              jsonPointers:
                - /status
              jqPathExpressions:
                - .status
            YAML
            "resource.customizations.ignoreDifferences.resources.signoz.io_Rule"      = <<-YAML
              jsonPointers:
                - /status
              jqPathExpressions:
                - .status
            YAML
            "resource.customizations.ignoreDifferences.resources.signoz.io_SavedView" = <<-YAML
              jsonPointers:
                - /status
              jqPathExpressions:
                - .status
            YAML
            "resource.customizations.health.generators.external-secrets.io_Password"  = <<-YAML
              hs = {}
              hs.status = "Healthy"
              return hs
            YAML
            "resource.ignoreResourceUpdatesEnabled"                                   = "true"
            "resource.customizations.ignoreResourceUpdates.batch_Job"                 = <<-YAML
              jsonPointers:
                - /status
            YAML
            "resource.customizations.ignoreResourceUpdates.batch_CronJob"             = <<-YAML
              jsonPointers:
                - /status
            YAML
          }
          secret = {
            extra = {
              "oidc.dex.clientSecret" = var.oidc_client_secret
            }
          }
          rbac = {
            "policy.csv"     = join("\n", concat([for group in var.admin_rbac_groups : "g, ${group}, role:admin"], [""]))
            "policy.default" = "role:readonly"
            "scopes"         = "[email, groups]"
          }
        }
        notifications = {
          enabled = false
        }
        server = {
          insecure = true
          resources = {
            requests = {
              cpu    = "50m"
              memory = "128Mi"
            }
            limits = {
              cpu    = "2000m"
              memory = "1Gi"
            }
          }
        }
        controller = {
          # SigNoz scrapes the application controller's sync and health metrics for the sync-failure alert.
          podAnnotations = {
            "signoz.io/path"   = "/metrics"
            "signoz.io/port"   = "8082"
            "signoz.io/scrape" = "true"
          }
          resources = {
            requests = {
              cpu    = "250m"
              memory = "512Mi"
            }
            limits = {
              cpu    = "4000m"
              memory = "6Gi"
            }
          }
        }
        repoServer = {
          resources = {
            requests = {
              cpu    = "100m"
              memory = "256Mi"
            }
            limits = {
              cpu    = "4000m"
              memory = "3Gi"
            }
          }
        }
        redis = {
          resources = {
            requests = {
              cpu    = "50m"
              memory = "128Mi"
            }
            limits = {
              cpu    = "1000m"
              memory = "2Gi"
            }
          }
        }
        applicationSet = {
          resources = {
            requests = {
              cpu    = "50m"
              memory = "128Mi"
            }
            limits = {
              cpu    = "2000m"
              memory = "1Gi"
            }
          }
        }
        dex = {
          resources = {
            requests = {
              cpu    = "25m"
              memory = "64Mi"
            }
            limits = {
              cpu    = "1000m"
              memory = "512Mi"
            }
          }
        }
      })
    ],
    local.needs_git_redirect ? [
      yamlencode({
        extraObjects = [
          {
            apiVersion = "v1"
            kind       = "ConfigMap"
            metadata = {
              name      = "argocd-gitconfig"
              namespace = local.namespace
            }
            data = {
              gitconfig = <<-EOT
                [url "${var.git_repo_url}"]
                	insteadOf = ${var.upstream_git_repo_url}
              EOT
            }
          }
        ]
        repoServer = {
          volumes = [
            {
              name = "gitconfig"
              configMap = {
                name = "argocd-gitconfig"
              }
            }
          ]
          volumeMounts = [
            {
              name      = "gitconfig"
              mountPath = "/etc/gitconfig"
              subPath   = "gitconfig"
            }
          ]
        }
        applicationSet = {
          extraVolumes = [
            {
              name = "gitconfig"
              configMap = {
                name = "argocd-gitconfig"
              }
            }
          ]
          extraVolumeMounts = [
            {
              name      = "gitconfig"
              mountPath = "/etc/gitconfig"
              subPath   = "gitconfig"
            }
          ]
        }
      })
    ] : []
  )
}

resource "kubernetes_secret_v1" "git_repo_creds" {
  count      = var.enable_git_repo_creds ? 1 : 0
  depends_on = [helm_release.argocd]

  metadata {
    name      = "argocd-repo-creds-git"
    namespace = local.namespace
    labels = {
      "argocd.argoproj.io/secret-type" = "repo-creds"
    }
  }

  data = {
    type          = "git"
    url           = coalesce(var.git_repo_creds_url, var.git_repo_url)
    sshPrivateKey = var.git_ssh_private_key
  }
}

resource "kubernetes_secret_v1" "control_registration" {
  depends_on = [helm_release.argocd]

  metadata {
    name      = "cluster-${var.cluster_name}"
    namespace = local.namespace
    annotations = merge(
      {
        "access-domain"     = local.cluster_domain
        "cluster-domain"    = local.cluster_domain
        "domain"            = coalesce(var.intranet_domain_name, var.domain_name)
        "intranet-domain"   = coalesce(var.intranet_domain_name, var.domain_name)
        "public-domain"     = var.public_domain_name
        "git-repo-url"      = local.git_identity_url
        "git-transport-url" = var.git_repo_url
      },
      try(var.annotations != null ? var.annotations : {}, {}),
      {
        "registered-cells" = local.registered_cells
      },
      try(length(trimspace(var.service_cidr)) > 0, false) ? {
        "service-cidr" = var.service_cidr
      } : {},
      try(length(trimspace(var.control_gateway_ipv4)) > 0, false) ? {
        "control-gateway-ipv4" = var.control_gateway_ipv4
      } : {},
      try(length(trimspace(var.tailscale_oauth_key)) > 0, false) ? {
        "tailscale-oauth-key" = var.tailscale_oauth_key
      } : {}
    )
    labels = merge(
      {
        "argocd.argoproj.io/secret-type" = "cluster"
        "environment"                    = var.cluster_environment
        "profile"                        = var.fleet_availability == "standalone" ? "minimal" : "production"
        "provider"                       = var.cluster_provider
        "role"                           = "ctrl"
      },
      var.cluster_labels
    )
  }

  data = {
    name   = var.cluster_name
    server = "https://kubernetes.default.svc"
    config = jsonencode({
      tlsClientConfig = {
        insecure = false
      }
    })
  }
}

resource "kubernetes_secret_v1" "cell_registration" {
  for_each = { for c in var.registered_cells : c.name => c }

  depends_on = [helm_release.argocd]

  metadata {
    name      = "cluster-${each.key}"
    namespace = local.namespace
    annotations = merge(
      {
        "access-domain"        = local.cluster_domain
        "cluster-domain"       = local.cluster_domain
        "control-cluster-name" = var.cluster_name
        "domain"               = coalesce(var.intranet_domain_name, var.domain_name)
        "intranet-domain"      = coalesce(var.intranet_domain_name, var.domain_name)
        "public-domain"        = var.public_domain_name
        "git-repo-url"         = local.git_identity_url
        "git-transport-url"    = var.git_repo_url
      },
      try(var.annotations["aws-account-id"] != null ? { "aws-account-id" = var.annotations["aws-account-id"] } : {}, {}),
      try(var.annotations["ecr-registry"] != null ? { "ecr-registry" = var.annotations["ecr-registry"] } : {}, {}),
      try(each.value.annotations != null ? each.value.annotations : {}, {}),
      {
        "registered-cells" = local.registered_cells
      }
    )
    labels = merge(
      {
        "argocd.argoproj.io/secret-type" = "cluster"
        "environment"                    = coalesce(each.value.environment, "production")
        "profile"                        = coalesce(try(each.value.profile, null), var.fleet_availability == "standalone" ? "minimal" : "production")
        "provider"                       = coalesce(each.value.provider, "floci")
        "role"                           = "cell"
      },
      try(each.value.labels, {})
    )
  }

  data = {
    name   = each.value.name
    server = each.value.endpoint
    config = jsonencode(merge(
      {
        tlsClientConfig = {
          insecure = false
          caData   = each.value.ca_certificate
        }
      },
      try(length(each.value.token) > 0, false) ? {
        bearerToken = each.value.token
      } : {},
      try(each.value.aws_auth_config != null, false) ? {
        awsAuthConfig = {
          clusterName = each.value.aws_auth_config.cluster_name
          roleARN     = each.value.aws_auth_config.role_arn
        }
      } : {},
      try(each.value.exec_provider_config != null, false) ? {
        execProviderConfig = {
          command    = each.value.exec_provider_config.command
          args       = each.value.exec_provider_config.args
          apiVersion = each.value.exec_provider_config.api_version
        }
      } : {}
    ))
  }
}

resource "kubernetes_role_v1" "workspace_snapshot_catalog_cluster_reader" {
  depends_on = [helm_release.argocd]

  metadata {
    name      = "workspace-snapshot-catalog-cluster-reader"
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/component" = "workspace-snapshot-catalog"
    }
  }

  rule {
    api_groups = [""]
    resources  = ["secrets"]
    resource_names = concat(
      [kubernetes_secret_v1.control_registration.metadata[0].name],
      sort([for c in var.registered_cells : "cluster-${c.name}"]),
    )
    verbs = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "workspace_snapshot_catalog_cluster_reader" {
  depends_on = [helm_release.argocd]

  metadata {
    name      = "workspace-snapshot-catalog-cluster-reader"
    namespace = local.namespace
    annotations = {
      "ignore-check.kube-linter.io/access-to-secrets" = "The workspace snapshot catalog requires get-only read access to named cluster connection secrets to aggregate snapshot metadata across cells."
    }
    labels = {
      "app.kubernetes.io/component" = "workspace-snapshot-catalog"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.workspace_snapshot_catalog_cluster_reader.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "workspace-snapshot-catalog"
    namespace = "coder-workspace-backup-system"
  }
}

resource "helm_release" "fleet_root" {
  name      = "fleet-root"
  chart     = "${path.module}/bootstrap_chart"
  namespace = local.namespace

  values = [
    yamlencode({
      objects = [
        {
          apiVersion = "argoproj.io/v1alpha1"
          kind       = "Application"
          metadata = {
            name      = "fleet-root"
            namespace = local.namespace
          }
          spec = {
            project = "default"
            source = {
              repoURL        = local.git_identity_url
              targetRevision = var.target_revision
              path           = "src/infra/argocd/apps"
              directory = {
                exclude = "profiles.yaml"
              }
            }
            destination = {
              server    = "https://kubernetes.default.svc"
              namespace = local.namespace
            }
            syncPolicy = {
              automated = {
                prune    = true
                selfHeal = true
              }
            }
          }
        }
      ]
    })
  ]

  depends_on = [helm_release.argocd]
}
