# Fetches Coder workspace metadata, owner identity details, and external attestation attributes for templates.

data "coder_workspace" "me" {}

data "coder_workspace_owner" "me" {}

# Coder keeps the OAuth token server-side and gives Git a transient ASKPASS
# helper. Referencing access_token here would instead persist it in workspace
# Terraform state or Kubernetes resources.
# tflint-ignore: terraform_unused_declarations
data "coder_external_auth" "github" {
  id = "github"
}

# Prompts workspace creators to link Dex at workspace creation without persisting tokens in state.
# tflint-ignore: terraform_unused_declarations
data "coder_external_auth" "dex" {
  id = "dex"
}

data "external" "attested_owner" {
  program = ["${path.module}/hooks/attest-owner.sh"]

  query = {
    binding_url = "https://headscale-workspace-registration.headscale.svc.cluster.local:8443/v1/bind"
  }
}

data "external" "workspace_build_context" {
  program = ["${path.module}/hooks/workspace-build-context.sh"]
}

data "external" "snapshot_repository_password" {
  program = ["${path.module}/hooks/snapshot-repository-password.sh"]

  query = {
    owner_id = local.owner_id
  }
}

resource "terraform_data" "attested_owner" {
  input = local.current_owner_attestation

  lifecycle {
    # Coder supplies the owner session and upstream OIDC tokens only for a real
    # start. Retain the first non-secret binding result for stop and delete.
    ignore_changes = [input]
  }
}

data "kubernetes_config_map_v1" "workspace_origin" {
  count = local.template_preview ? 0 : 1

  metadata {
    name      = "coder-workspace-origin"
    namespace = local.workspace_namespace
  }
}
