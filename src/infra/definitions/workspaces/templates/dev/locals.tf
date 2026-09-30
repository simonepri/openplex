# Computes naming conventions, resource quotas, cell endpoints, and runtime flags for Coder workspace templates.

locals {
  current_owner_attestation = data.external.attested_owner.result
  template_preview          = try(local.current_owner_attestation.preview, "false") == "true"
  owner_binding = (
    local.current_owner_attestation.attested == "true" || local.template_preview
  ) ? local.current_owner_attestation : terraform_data.attested_owner.output
  owner_id       = local.owner_binding.id
  owner_email    = local.owner_binding.email
  owner_username = local.owner_binding.preferred_username
  workspace_start_count = (
    data.coder_workspace.me.start_count == 1 &&
    (local.current_owner_attestation.attested == "true" || local.template_preview)
  ) ? 1 : 0
  app_labels = {
    "app.kubernetes.io/instance" = "coder-workspace-${data.coder_workspace.me.id}"
    # LINT.IfChange(coder-workspace-infrastructure-identity)
    "app.kubernetes.io/name"    = "coder-workspace"
    "app.kubernetes.io/part-of" = "coder"
    "com.coder.resource"        = "true"
    # LINT.ThenChange(//src/infra/argocd/components/buildbuddy_cache/kustomize/network-policy.yaml:coder-workspace-infrastructure-identity)
    "com.coder.user.id"        = local.owner_id
    "com.coder.user.username"  = local.owner_username
    "com.coder.workspace.id"   = data.coder_workspace.me.id
    "com.coder.workspace.name" = data.coder_workspace.me.name
  }
  workspace_scheduling_labels = {
    "availability-class"        = "ha"
    "kueue.x-k8s.io/queue-name" = "ha"
    "latency-class"             = "ls"
  }
  workspace_pod_scheduling_labels = merge(local.workspace_scheduling_labels, {
    "kueue.x-k8s.io/managed" = "true"
  })
  torchinductor_environment = {
    TORCHINDUCTOR_CACHE_DIR             = "/tmp/cache/torchinductor"
    TORCHINDUCTOR_FX_GRAPH_CACHE        = "1"
    TORCHINDUCTOR_FX_GRAPH_REMOTE_CACHE = "1"
  }
  workspace_cache_environment = {
    BAZEL_OUTPUT_ROOT = "/tmp/bazel"
    CARGO_TARGET_DIR  = "/tmp/cargo-target"
    GOCACHE           = "/tmp/cache/go-build"
    XDG_CACHE_HOME    = "/tmp/cache"
  }
  # LINT.IfChange(workspace-sidecar-capacity-reserve)
  workspace_sidecar_requests = {
    backup_proxy = { cpu = "25m", memory = "64Mi" }
    tailnet      = { cpu = "10m", memory = "32Mi" }
  }
  # LINT.ThenChange(//src/bazel/checks/records/check_team_records.py:workspace-sidecar-capacity-reserve)

  workspace_placement_inventory = try(jsondecode(var.workspace_placement_inventory), {})
  workspace_placement_inventory_valid = try(
    length(local.workspace_placement_inventory) > 0 && alltrue([
      for cluster, envelope in local.workspace_placement_inventory :
      can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", cluster)) &&
      (
        toset(keys(envelope)) == toset(["accelerator_offers", "cpu", "gpu_offers", "memory_gib", "storage_gib", "tmp_storage_gib"]) ||
        toset(keys(envelope)) == toset(["accelerator_offers", "cpu", "gpu_offers", "memory_gib", "storage_gib"]) ||
        toset(keys(envelope)) == toset(["cpu", "gpu_offers", "memory_gib", "storage_gib", "tmp_storage_gib"]) ||
        toset(keys(envelope)) == toset(["cpu", "gpu_offers", "memory_gib", "storage_gib"])
      ) &&
      alltrue([
        for resource in [envelope.cpu, envelope.memory_gib, envelope.storage_gib] :
        toset(keys(resource)) == toset(["default", "max", "min"]) &&
        resource.min >= 1 &&
        resource.min == floor(resource.min) &&
        resource.default == floor(resource.default) &&
        resource.max == floor(resource.max) &&
        resource.min <= resource.default &&
        resource.default <= resource.max
      ]) &&
      alltrue([
        for offer_key, offer in try(envelope.gpu_offers, {}) :
        contains(keys(local.gpu_catalog_offers), offer_key) &&
        toset(keys(offer)) == toset(["capacity_type", "max_count", "model", "workspace_max"]) &&
        offer_key == "${offer.model}-${offer.capacity_type}" &&
        contains(keys(local.gpu_catalog.models), offer.model) &&
        contains(keys(local.gpu_catalog.capacity_types), offer.capacity_type) &&
        offer.max_count >= 1 &&
        offer.max_count == floor(offer.max_count) &&
        toset(keys(offer.workspace_max)) == toset(["cpu", "memory_gib"]) &&
        offer.workspace_max.cpu >= envelope.cpu.default &&
        offer.workspace_max.cpu == floor(offer.workspace_max.cpu) &&
        offer.workspace_max.memory_gib >= envelope.memory_gib.default &&
        offer.workspace_max.memory_gib == floor(offer.workspace_max.memory_gib)
      ]) &&
      alltrue([
        for _, offer in try(envelope.accelerator_offers, {}) :
        toset(keys(offer)) == toset([
          "capacity_type",
          "class",
          "kind",
          "max_count",
          "node_selector",
          "resource",
          "taint",
          "workspace_max",
        ]) &&
        contains(keys(local.accelerator_kind_names), offer.kind) &&
        can(regex("^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", offer.class)) &&
        contains(keys(local.gpu_catalog.capacity_types), offer.capacity_type) &&
        offer.resource == (offer.kind == "gpu" ? "nvidia.com/gpu" : "google.com/tpu") &&
        offer.node_selector == {
          key   = "${offer.kind}-class"
          value = offer.class
        } &&
        offer.taint == {
          effect = "NoSchedule"
          key    = offer.resource
          value  = "present"
        } &&
        offer.max_count >= 1 &&
        offer.max_count == floor(offer.max_count) &&
        toset(keys(offer.workspace_max)) == toset(["cpu", "memory_gib"]) &&
        offer.workspace_max.cpu >= envelope.cpu.default &&
        offer.workspace_max.cpu == floor(offer.workspace_max.cpu) &&
        offer.workspace_max.memory_gib >= envelope.memory_gib.default &&
        offer.workspace_max.memory_gib == floor(offer.workspace_max.memory_gib)
      ])
    ]),
    false,
  )
  workspace_incarnation_inventory = try(jsondecode(var.workspace_incarnation_inventory), {})
  workspace_incarnation_inventory_valid = try(
    length(local.workspace_incarnation_inventory) > 0 &&
    toset(keys(local.workspace_incarnation_inventory)) == toset(keys(local.workspace_placement_inventory)) &&
    alltrue([
      for cluster, incarnation in local.workspace_incarnation_inventory :
      can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", cluster)) &&
      can(regex("^[0-9a-f]{12}$", incarnation))
    ]),
    false,
  )
  workspace_virtual_name_inventory = try(jsondecode(var.workspace_virtual_name_inventory), {})
  workspace_virtual_name_inventory_valid = try(
    length(local.workspace_virtual_name_inventory) > 0 &&
    toset(keys(local.workspace_virtual_name_inventory)) == toset(keys(local.workspace_placement_inventory)) &&
    alltrue([
      for cluster, virtual_name in local.workspace_virtual_name_inventory :
      can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", cluster)) &&
      can(regex("^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", virtual_name))
    ]),
    false,
  )
  selected_cell = data.coder_parameter.cluster.value
  selected_workspace_placement = try(
    local.workspace_placement_inventory[local.selected_cell],
    {
      cpu             = { default = 1, max = 1, min = 1 }
      gpu_offers      = {}
      memory_gib      = { default = 1, max = 1, min = 1 }
      storage_gib     = { default = 8, max = 48, min = 8 }
      tmp_storage_gib = 64
    },
  )
  selected_incarnation   = try(local.workspace_incarnation_inventory[local.selected_cell], "")
  selected_virtual_name  = try(local.workspace_virtual_name_inventory[local.selected_cell], "")
  workspace_cpu          = local.selected_workspace_placement.cpu
  workspace_memory       = local.selected_workspace_placement.memory_gib
  workspace_storage      = local.selected_workspace_placement.storage_gib
  workspace_tmp_size_gib = try(local.selected_workspace_placement.tmp_storage_gib, 64)
  workspace_tmp_size     = "${local.workspace_tmp_size_gib}Gi"

  workspace_namespace     = var.workspace_namespace
  workspace_origin_object = try(one(data.kubernetes_resources.workspace_origin[0].objects), null)
  workspace_origin_data   = try(local.workspace_origin_object.data, {})
  selected_workload_origin = local.template_preview ? {
    authMode       = var.workload_origin_auth_mode
    cell           = var.cell
    originRegion   = var.workload_origin_region
    originRegistry = var.workload_registry
    provider       = var.workload_origin_provider
    roleArn        = var.workload_origin_role_arn
    tokenAudience  = var.workload_origin_token_audience
    tokenFile      = var.workload_origin_token_file
  } : local.workspace_origin_data
  workload_origin_auth_mode      = try(local.selected_workload_origin.authMode, "")
  workload_origin_provider       = try(local.selected_workload_origin.provider, "")
  workload_origin_region         = try(local.selected_workload_origin.originRegion, "")
  workload_origin_role_arn       = try(local.selected_workload_origin.roleArn, "")
  workload_origin_token_audience = try(local.selected_workload_origin.tokenAudience, "")
  workload_origin_token_file     = try(local.selected_workload_origin.tokenFile, "")
  workload_registry              = try(local.selected_workload_origin.originRegistry, "")
  workload_registry_insecure     = local.template_preview ? var.workload_registry_insecure : "false"
  workload_origin_environment = merge({
    AWS_ECR_DISABLE_CACHE      = "true"
    WORKLOAD_REGISTRY          = local.workload_registry
    WORKLOAD_REGISTRY_INSECURE = local.workload_registry_insecure
    # floci-divergence: Floci clusters do not require workload registry credential helper env vars.
    }, local.workload_origin_auth_mode == "floci" ? {} : {
    AWS_DEFAULT_REGION        = local.workload_origin_region
    AWS_EC2_METADATA_DISABLED = "true"
    AWS_REGION                = local.workload_origin_region
    }, local.workload_origin_auth_mode == "web-identity" ? {
    AWS_ROLE_ARN                = local.workload_origin_role_arn
    AWS_WEB_IDENTITY_TOKEN_FILE = local.workload_origin_token_file
  } : {})

  # Machine naming derivation (inlined from workspace_machine)
  readable_workspace_machine = "${local.owner_username}-${data.coder_workspace.me.name}"
  workspace_machine_is_readable = (
    local.owner_username != "ws" &&
    can(regex("^[a-z][a-z0-9]*$", local.owner_username)) &&
    can(regex("^[a-z0-9]+(?:-[a-z0-9]+)*$", data.coder_workspace.me.name)) &&
    length(local.readable_workspace_machine) <= 63 &&
    can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", local.readable_workspace_machine))
  )
  workspace_machine = local.workspace_machine_is_readable ? local.readable_workspace_machine : "ws-${substr(sha256(join("\u0000", [
    "workspace-machine",
    local.owner_id,
    data.coder_workspace.me.id,
  ])), 0, 40)}"

  ssh_port               = 2222
  ssh_uri                = "ssh://${local.owner_username}@${local.ssh_hostname}:${local.ssh_port}"
  ssh_enabled            = data.coder_parameter.ssh_enabled.value == "true" && data.coder_parameter.ssh_public_key.value != ""
  active_ctrl_name       = try(regex("^https?://headscale\\.([^.]+)", var.headscale_url)[0], "")
  coder_agent_url        = "https://coder.${var.access_alias_domain}"
  coder_access_authority = split("/", replace(data.coder_workspace.me.access_url, "/^https?:\\/\\//", ""))[0]
  coder_access_host      = split(":", local.coder_access_authority)[0]
  coder_app_host_suffix  = ".${local.coder_access_host}"
  coder_owner_name       = data.coder_workspace_owner.me.name
  paseo_app_hostname     = "paseo--${data.coder_workspace.me.name}--${local.coder_owner_name}${local.coder_app_host_suffix}"
  zasper_access_token    = sha256("${data.coder_workspace.me.id}:zasper")
  coder_app_label_length = length(join("--", [
    "vscode",
    data.coder_workspace.me.name,
    local.coder_owner_name,
  ]))
  coder_agent_init_script = replace(
    replace(
      coder_agent.main.init_script,
      "/(?m)^BINARY_URL=.*$/",
      "BINARY_URL=${local.coder_agent_url}/bin/coder-linux-${var.workspace_arch}",
    ),
    "/(?m)^export CODER_AGENT_URL=.*$/",
    "export CODER_AGENT_URL=\"${local.coder_agent_url}/\"",
  )

  checkout_segments = slice(split("/", var.checkout_path), 1, length(split("/", var.checkout_path)))
  reserved_checkout_roots = [
    # keep-sorted start
    "bin",
    "boot",
    "dev",
    "etc",
    "home",
    "lib",
    "lib64",
    "proc",
    "run",
    "sbin",
    "sys",
    "tmp",
    "usr",
    "var",
    # keep-sorted end
  ]
}
