# Configures team-scoped and legacy S3 storage credentials and ExternalSecret manifests.

data "kubernetes_resources" "workspace_s3_grants" {
  count = local.template_preview ? 0 : 1

  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = local.workspace_namespace
  label_selector = "app.kubernetes.io/component=workspace-s3-grant"
}

# LINT.IfChange(workspace-s3-grant-contract)
locals {
  workspace_s3_grants        = try(data.kubernetes_resources.workspace_s3_grants[0].objects, [])
  workspace_s3_legacy_grants = [for g in local.workspace_s3_grants : g if try(g.data.grant, "") == "legacy"]
  workspace_s3_team_grants = {
    for g in local.workspace_s3_grants : g.data.team => g
    if try(g.data.grant, "") == "team" &&
       !startswith(try(g.metadata.name, ""), "workspace-s3-read-grant-team-") &&
       contains(split(",", try(g.data.members, "")), local.owner_id)
  }
  workspace_s3_teams        = sort(keys(local.workspace_s3_team_grants))
  workspace_s3_secret_name  = "coder-${data.coder_workspace.me.id}-s3"
  workspace_s3_global_teams = [for t in local.workspace_s3_teams : t if try(local.workspace_s3_team_grants[t].data.globalStorage, "") == "true"]

  workspace_s3_read_grants = {
    for g in local.workspace_s3_grants : g.data.team => g
    if (startswith(try(g.metadata.name, ""), "workspace-s3-read-grant-team-") || try(g.data.readers, "") == "all") &&
       try(g.data.readers, "all") == "all" &&
       try(g.data.team, "") != ""
  }
  workspace_s3_reader_teams        = sort([for t in keys(local.workspace_s3_read_grants) : t if !contains(local.workspace_s3_teams, t)])
  workspace_s3_reader_global_teams = [for t in local.workspace_s3_reader_teams : t if try(local.workspace_s3_read_grants[t].data.globalStorage, "") == "true"]
}
# LINT.ThenChange(//src/infra/argocd/components/s3_gateway_team/helm/templates/workspace-grant.yaml:workspace-s3-grant-contract,//src/infra/argocd/components/coder_workspaces/helm/templates/workspace-s3-grant-legacy.yaml:workspace-s3-grant-contract,//src/infra/argocd/components/kyverno/kustomize/workspace-s3-grant-policy.yaml:workspace-s3-grant-contract)

locals {
  workspace_s3_volumes = merge(
    { for t in local.workspace_s3_teams : "home-${t}" => { remote = "home-${t}", read_only = false } },
    { for t in local.workspace_s3_teams : "scratch-${t}" => { remote = "scratch-${t}", read_only = false } },
    { for t in local.workspace_s3_global_teams : "global-home-${t}" => { remote = "global-home-${t}", read_only = false } },
    { for t in local.workspace_s3_global_teams : "global-scratch-${t}" => { remote = "global-scratch-${t}", read_only = false } },
    { for t in local.workspace_s3_global_teams : "global-meta-${t}" => { remote = "global-meta-${t}", read_only = false } },
    length(local.workspace_s3_teams) > 0 ? { "meta" = { remote = "meta", read_only = true } } : {},

    { for t in local.workspace_s3_reader_teams : "home-${t}" => { remote = "home-${t}", read_only = true } },
    { for t in local.workspace_s3_reader_teams : "scratch-${t}" => { remote = "scratch-${t}", read_only = true } },
    { for t in local.workspace_s3_reader_global_teams : "global-home-${t}" => { remote = "global-home-${t}", read_only = true } },
    { for t in local.workspace_s3_reader_global_teams : "global-scratch-${t}" => { remote = "global-scratch-${t}", read_only = true } },
    { for t in local.workspace_s3_reader_global_teams : "global-meta-${t}" => { remote = "global-meta-${t}", read_only = true } },
  )

  workspace_s3_credentials_default_access_key = length(local.workspace_s3_teams) > 0 ? "{{ .team_${local.workspace_s3_teams[0]}_access_key_id }}" : "{{ .legacy_access_key_id }}"
  workspace_s3_credentials_default_secret_key = length(local.workspace_s3_teams) > 0 ? "{{ .team_${local.workspace_s3_teams[0]}_secret_access_key }}" : "{{ .legacy_secret_access_key }}"

  workspace_s3_credentials = join("\n", concat(
    [
      "[default]",
      "aws_access_key_id = ${local.workspace_s3_credentials_default_access_key}",
      "aws_secret_access_key = ${local.workspace_s3_credentials_default_secret_key}",
    ],
    flatten([
      for t in local.workspace_s3_teams : [
        "",
        "[${t}]",
        "aws_access_key_id = {{ .team_${t}_access_key_id }}",
        "aws_secret_access_key = {{ .team_${t}_secret_access_key }}",
      ]
    ]),
    flatten([
      for t in local.workspace_s3_reader_teams : [
        "",
        "[${t}]",
        "aws_access_key_id = {{ .team_${t}_access_key_id }}",
        "aws_secret_access_key = {{ .team_${t}_secret_access_key }}",
      ]
    ]),
    [
      "",
      "[legacy-research-data]",
      "aws_access_key_id = {{ .legacy_access_key_id }}",
      "aws_secret_access_key = {{ .legacy_secret_access_key }}",
      "",
    ],
  ))

  workspace_s3_config_data = join("\n", concat(
    [
      "[gateway-legacy]",
      "type = s3",
      "provider = Other",
      "access_key_id = {{ .legacy_access_key_id }}",
      "secret_access_key = {{ .legacy_secret_access_key }}",
      "endpoint = http://s3-gateway.s3-system.svc",
      "force_path_style = true",
      "no_check_bucket = true",
      "",
      "[legacy]",
      "type = alias",
      "remote = gateway-legacy:${local.selected_virtual_name}/legacy",
      "",
      "[legacy-use1]",
      "type = alias",
      "remote = gateway-legacy:aws-use1/legacy",
    ],
    flatten([
      for t in local.workspace_s3_teams : concat(
        [
          "",
          "[gateway-${t}]",
          "type = s3",
          "provider = Other",
          "access_key_id = {{ .team_${t}_access_key_id }}",
          "secret_access_key = {{ .team_${t}_secret_access_key }}",
          "endpoint = http://s3-gateway.s3-system.svc",
          "force_path_style = true",
          "no_check_bucket = true",
          "",
          "[home-${t}]",
          "type = alias",
          "remote = gateway-${t}:${local.selected_virtual_name}/home/${t}",
          "",
          "[scratch-${t}]",
          "type = alias",
          "remote = gateway-${t}:${local.selected_virtual_name}/scratch/${t}",
        ],
        contains(local.workspace_s3_global_teams, t) ? [
          "",
          "[global-home-${t}]",
          "type = alias",
          "remote = gateway-${t}:global/home/${t}",
          "",
          "[global-scratch-${t}]",
          "type = alias",
          "remote = gateway-${t}:global/scratch/${t}",
          "",
          "[global-meta-${t}]",
          "type = alias",
          "remote = gateway-${t}:global/meta",
        ] : [],
      )
    ]),
    flatten([
      for t in local.workspace_s3_reader_teams : concat(
        [
          "",
          "[gateway-${t}]",
          "type = s3",
          "provider = Other",
          "access_key_id = {{ .team_${t}_access_key_id }}",
          "secret_access_key = {{ .team_${t}_secret_access_key }}",
          "endpoint = http://s3-gateway.s3-system.svc",
          "force_path_style = true",
          "no_check_bucket = true",
          "",
          "[home-${t}]",
          "type = alias",
          "remote = gateway-${t}:${local.selected_virtual_name}/home/${t}",
          "",
          "[scratch-${t}]",
          "type = alias",
          "remote = gateway-${t}:${local.selected_virtual_name}/scratch/${t}",
        ],
        contains(local.workspace_s3_reader_global_teams, t) ? [
          "",
          "[global-home-${t}]",
          "type = alias",
          "remote = gateway-${t}:global/home/${t}",
          "",
          "[global-scratch-${t}]",
          "type = alias",
          "remote = gateway-${t}:global/scratch/${t}",
          "",
          "[global-meta-${t}]",
          "type = alias",
          "remote = gateway-${t}:global/meta",
        ] : [],
      )
    ]),
    length(local.workspace_s3_teams) > 0 ? [
      "",
      "[meta]",
      "type = alias",
      "remote = gateway-${local.workspace_s3_teams[0]}:${local.selected_virtual_name}/meta",
    ] : [],
    [""],
  ))
}

# LINT.IfChange(workspace-s3-credential-contract)
resource "kubernetes_manifest" "workspace_s3_credentials" {
  count = (local.workspace_start_count == 1 && !local.template_preview) ? 1 : 0

  manifest = {
    apiVersion = "external-secrets.io/v1"
    kind       = "ExternalSecret"
    metadata = {
      name      = local.workspace_s3_secret_name
      namespace = local.workspace_namespace
      labels    = merge(local.app_labels, { "app.kubernetes.io/component" = "workspace-s3-credentials" })
    }
    spec = {
      refreshInterval = "1h"
      secretStoreRef = {
        kind = "ClusterSecretStore"
        name = "workspace-s3-workspaces"
      }
      target = {
        name           = local.workspace_s3_secret_name
        creationPolicy = "Owner"
        deletionPolicy = "Retain"
        template = {
          engineVersion = "v2"
          data = {
            credentials = local.workspace_s3_credentials
            configData  = local.workspace_s3_config_data
          }
        }
      }
      data = concat(
        [
          {
            secretKey = "legacy_access_key_id"
            remoteRef = {
              key      = try(local.workspace_s3_legacy_grants[0].data.recordName, "")
              property = "access_key_id"
            }
          },
          {
            secretKey = "legacy_secret_access_key"
            remoteRef = {
              key      = try(local.workspace_s3_legacy_grants[0].data.recordName, "")
              property = "secret_access_key"
            }
          },
        ],
        flatten([
          for t in local.workspace_s3_teams : [
            {
              secretKey = "team_${t}_access_key_id"
              remoteRef = {
                key      = local.workspace_s3_team_grants[t].data.recordName
                property = "access_key_id"
              }
            },
            {
              secretKey = "team_${t}_secret_access_key"
              remoteRef = {
                key      = local.workspace_s3_team_grants[t].data.recordName
                property = "secret_access_key"
              }
            },
          ]
        ]),
        flatten([
          for t in local.workspace_s3_reader_teams : [
            {
              secretKey = "team_${t}_access_key_id"
              remoteRef = {
                key      = local.workspace_s3_read_grants[t].data.recordName
                property = "access_key_id"
              }
            },
            {
              secretKey = "team_${t}_secret_access_key"
              remoteRef = {
                key      = local.workspace_s3_read_grants[t].data.recordName
                property = "secret_access_key"
              }
            },
          ]
        ])
      )
    }
  }

  wait {
    condition {
      type   = "Ready"
      status = "True"
    }
  }

  timeouts {
    create = "3m"
  }

  lifecycle {
    precondition {
      condition     = length(local.workspace_s3_legacy_grants) == 1
      error_message = "The selected cell must publish exactly one workspace-s3-grant-legacy ConfigMap."
    }
  }
}
# LINT.ThenChange(//src/infra/argocd/components/kyverno/kustomize/workspace-s3-grant-policy.yaml:workspace-s3-credential-contract)
