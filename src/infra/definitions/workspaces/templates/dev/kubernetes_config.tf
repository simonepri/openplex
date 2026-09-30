# Declares Kubernetes ConfigMaps, custom CA certificate mounts, and runtime configuration data for workspace pods.

data "kubernetes_resources" "workspace_cell_ca" {
  count          = (local.template_preview || var.ca_config_map_name == "") ? 0 : 1
  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = local.workspace_namespace
  field_selector = "metadata.name=${var.ca_config_map_name}"
}

locals {
  workspace_cell_ca_objects = try(data.kubernetes_resources.workspace_cell_ca[0].objects, [])
  workspace_cell_ca         = try(trimspace(one(local.workspace_cell_ca_objects).data["ca.crt"]), "")
  workspace_control_plane_ca = trimspace(
    base64decode(var.control_plane_ca_base64),
  )
  workspace_ca_bundle = join("\n", compact([
    local.workspace_cell_ca,
    local.workspace_control_plane_ca,
  ]))
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
        (
          length(local.workspace_cell_ca_objects) == 1 &&
          can(regex(
            "^(?:-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----[\\r\\n]*)+$",
            local.workspace_cell_ca,
          ))
        )
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

# This kubeconfig contains no bearer token. Kubernetes rotates the separately
# projected token and kubectl reads it for each request through tokenFile.
resource "kubernetes_config_map_v1" "workspace_kubeconfig" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-kubeconfig"
    namespace = local.workspace_namespace
    labels    = local.app_labels
  }

  data = {
    config = yamlencode({
      apiVersion = "v1"
      kind       = "Config"
      clusters = [{
        name = local.selected_cell
        cluster = {
          server                  = "https://kubernetes.default.svc"
          "certificate-authority" = "/var/run/workspace/kubernetes/ca.crt"
        }
      }]
      contexts = [{
        name = local.selected_cell
        context = {
          cluster   = local.selected_cell
          namespace = local.workspace_namespace
          user      = "coder-workspace"
        }
      }]
      "current-context" = local.selected_cell
      users = [{
        name = "coder-workspace"
        user = {
          tokenFile = "/var/run/workspace/kubernetes/token"
        }
      }]
    })
  }
}
