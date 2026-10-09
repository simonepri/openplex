# Declares Kubernetes ConfigMaps, custom CA certificate mounts, and runtime configuration data for workspace pods.

data "kubernetes_config_map_v1" "workspace_cell_ca" {
  count = (local.template_preview || var.ca_config_map_name == "") ? 0 : 1

  metadata {
    name      = var.ca_config_map_name
    namespace = local.workspace_namespace
  }
}

locals {
  workspace_cell_ca = try(trimspace(data.kubernetes_config_map_v1.workspace_cell_ca[0].data["ca.crt"]), "")
  workspace_control_plane_ca = trimspace(
    base64decode(var.control_plane_ca_base64),
  )
  workspace_peer_cell_cas = [for ca in values(jsondecode(var.cell_ca_inventory)) : trimspace(base64decode(ca))]
  workspace_ca_bundle = join("\n", distinct(compact(concat([
    local.workspace_cell_ca,
    local.workspace_control_plane_ca,
  ], local.workspace_peer_cell_cas))))
  # Every registered cell and the control plane, each reached through its own kube-oidc-proxy.
  workspace_kube_clusters = sort(distinct(compact(concat(
    [local.selected_cell, var.control_plane_name],
    keys(jsondecode(var.cell_ca_inventory)),
  ))))
}

resource "kubernetes_config_map_v1" "workspace_ca" {
  count = local.workspace_start_count

  metadata {
    name      = "coder-${data.coder_workspace.me.id}-ca"
    namespace = local.workspace_namespace
    labels    = local.app_labels
  }

  data = {
    "ca.crt" = local.workspace_ca_bundle
  }

  lifecycle {
    precondition {
      condition = (
        local.template_preview ||
        var.ca_config_map_name == "" ||
        can(regex(
          "^(?:-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----[\\r\\n]*)+$",
          local.workspace_cell_ca,
        ))
      )
      error_message = "The selected cell CA ConfigMap must contain valid PEM certificates in ca.crt."
    }
  }
}

resource "kubernetes_config_map_v1" "mise" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-mise"
    namespace = local.workspace_namespace
    labels    = local.app_labels
  }

  data = {
    "config.toml"               = file("${path.module}/container/config/workspace-mise.toml")
    "workspace-herdr.toml"      = file("${path.module}/container/config/workspace-herdr.toml")
    "workspace-snazzy.zsh"      = file("${path.module}/container/config/workspace-snazzy.zsh")
    "workspace-zellij.kdl"      = file("${path.module}/container/config/workspace-zellij.kdl")
    "workspace.zsh_plugins.txt" = file("${path.module}/container/config/workspace.zsh_plugins.txt")
    "workspace.zshrc"           = file("${path.module}/container/config/workspace.zshrc")
    "zig-cc.sh"                 = file("${path.module}/container/config/zig-cc.sh")
    "kopia-restore.sh"          = module.coder_snapshots.restore_script
  }
}

resource "kubernetes_config_map_v1" "access" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-access"
    namespace = local.workspace_namespace
    labels    = local.app_labels
  }

  data = {
    "code-server.sh"         = file("${path.module}/container/apps/code-server.sh")
    "coder-cli.sh"           = file("${path.module}/container/apps/coder-cli.sh")
    "paseo_coder_proxy.py"   = file("${path.module}/container/apps/paseo_coder_proxy.py")
    "paseo_instructions.py"  = file("${path.module}/container/apps/paseo_instructions.py")
    "reboot.sh"              = file("${path.module}/container/apps/reboot.sh")
    "s3i-cli.sh"             = file("${path.module}/container/apps/s3i-cli.sh")
    "workspace-herdr.sh"     = file("${path.module}/container/apps/workspace-herdr.sh")
    "workspace-identity.sh"  = file("${path.module}/container/init/workspace-identity.sh")
    "workspace-mounts.sh"    = file("${path.module}/container/init/workspace-mounts.sh")
    "workspace-nohang.sh"    = file("${path.module}/container/apps/workspace-nohang.sh")
    "workspace_nohang.py"    = file("${path.module}/container/apps/workspace_nohang.py")
    "workspace-paseo.sh"     = file("${path.module}/container/apps/workspace-paseo.sh")
    "workspace-shell.sh"     = file("${path.module}/container/init/workspace-shell.sh")
    "workspace-snapshots.sh" = file("${path.module}/container/init/workspace-snapshots.sh")
    "workspace-ssh.sh"       = file("${path.module}/container/sidecars/workspace-ssh.sh")
    "workspace-tailnet.sh"   = file("${path.module}/container/sidecars/workspace-tailnet.sh")
    "workspace-zellij.sh"    = file("${path.module}/container/apps/workspace-zellij.sh")
  }
}

# Generates the in-pod kubeconfig with one context per cluster kube-oidc-proxy door; the selected cell is current.
# The user's Dex identity is fetched on demand via Coder external-auth exec plugin.
resource "kubernetes_config_map_v1" "workspace_kubeconfig" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-kubeconfig"
    namespace = local.workspace_namespace
    labels    = local.app_labels
  }

  data = {
    kubeconfig = yamlencode({
      apiVersion  = "v1"
      kind        = "Config"
      preferences = {}
      clusters = [for cluster in local.workspace_kube_clusters : {
        name = cluster
        cluster = {
          server                  = "https://kube-oidc-proxy.${cluster}.${var.access_alias_domain}"
          "certificate-authority" = "/etc/workspace/ca/ca.crt"
        }
      }]
      contexts = [for cluster in local.workspace_kube_clusters : {
        name = cluster
        context = {
          cluster = cluster
          user    = local.owner_username
        }
      }]
      "current-context" = local.selected_cell
      users = [{
        name = local.owner_username
        user = {
          exec = {
            apiVersion = "client.authentication.k8s.io/v1"
            command    = "/bin/sh"
            args = [
              "-c",
              "out=$(coder external-auth access-token dex --extra id_token 2>&1) || { printf '%s\\n' \"$out\" >&2; exit 1; }\n[ -n \"$out\" ] || { echo 'Missing ID token from Coder external auth dex' >&2; exit 1; }\nprintf '{\"apiVersion\":\"client.authentication.k8s.io/v1\",\"kind\":\"ExecCredential\",\"status\":{\"token\":\"%s\"}}\\n' \"$out\""
            ]
            interactiveMode    = "Never"
            provideClusterInfo = false
          }
        }
      }]
    })
  }
}
