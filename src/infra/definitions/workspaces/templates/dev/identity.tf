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

data "external" "attested_owner" {
  program = ["${path.module}/hooks/attest-owner.sh"]

  query = {
    binding_url = "https://headscale-workspace-registration.headscale.svc.cluster.local:8443/v1/bind"
  }
}

data "external" "workspace_build_context" {
  program = ["${path.module}/hooks/workspace-build-context.sh"]
}

resource "terraform_data" "attested_owner" {
  input = local.current_owner_attestation

  lifecycle {
    # Coder supplies the owner session and upstream OIDC tokens only for a real
    # start. Retain the first non-secret binding result for stop and delete.
    ignore_changes = [input]
  }
}

data "kubernetes_resources" "workspace_origin" {
  count          = local.template_preview ? 0 : 1
  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = local.workspace_namespace
  field_selector = "metadata.name=coder-workspace-origin"
}
