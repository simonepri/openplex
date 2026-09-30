# Installs Argo CD via Helm and provisions seed AppProjects, cluster registration secrets, and root fleet applications.

locals {
  namespace          = "argocd"
  release            = "argocd"
  needs_git_redirect = var.git_repo_url != var.upstream_git_repo_url
  git_identity_url   = try(var.annotations["git-repo-url"], var.git_repo_url)
  cluster_domain     = coalesce(var.cluster_domain_name, var.access_domain_name, "c.${var.intranet_domain_name}")
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
              issuer          = coalesce(var.oidc_issuer, "https://dex.${local.cluster_domain}")
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
    labels = {
      "argocd.argoproj.io/secret-type" = "cluster"
      "environment"                    = var.cluster_environment
      "profile"                        = var.fleet_availability == "standalone" ? "minimal" : "production"
      "provider"                       = var.cluster_provider
      "role"                           = "ctrl"
    }
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
        "access-domain"     = local.cluster_domain
        "cluster-domain"    = local.cluster_domain
        "domain"            = coalesce(var.intranet_domain_name, var.domain_name)
        "intranet-domain"   = coalesce(var.intranet_domain_name, var.domain_name)
        "public-domain"     = var.public_domain_name
        "git-repo-url"      = local.git_identity_url
        "git-transport-url" = var.git_repo_url
        "registered-cells"  = join(",", sort([for cell in var.registered_cells : cell.name]))
      },
      try(var.annotations["aws-account-id"] != null ? { "aws-account-id" = var.annotations["aws-account-id"] } : {}, {}),
      try(var.annotations["ecr-registry"] != null ? { "ecr-registry" = var.annotations["ecr-registry"] } : {}, {}),
      try(each.value.annotations != null ? each.value.annotations : {}, {})
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
