# Defines GPU/TPU accelerator hardware catalogs, node affinity constraints, and compute placement contracts.

locals {
  gpu_catalog = {
    models = {
      "a10"                 = { architecture = "Ampere", display_name = "NVIDIA A10", memory_gb = 24 }
      "a100-40gb"           = { architecture = "Ampere", display_name = "NVIDIA A100 40 GB", memory_gb = 40 }
      "a100-80gb"           = { architecture = "Ampere", display_name = "NVIDIA A100 80 GB", memory_gb = 80 }
      "a10g"                = { architecture = "Ampere", display_name = "NVIDIA A10G", memory_gb = 24 }
      "b200"                = { architecture = "Blackwell", display_name = "NVIDIA B200", memory_gb = 180 }
      "b300"                = { architecture = "Blackwell", display_name = "NVIDIA B300", memory_gb = 288 }
      "h100"                = { architecture = "Hopper", display_name = "NVIDIA H100", memory_gb = 80 }
      "h100-nvl-94gb"       = { architecture = "Hopper", display_name = "NVIDIA H100 NVL 94 GB", memory_gb = 94 }
      "h200"                = { architecture = "Hopper", display_name = "NVIDIA H200", memory_gb = 141 }
      "l4"                  = { architecture = "Ada", display_name = "NVIDIA L4", memory_gb = 24 }
      "l40s"                = { architecture = "Ada", display_name = "NVIDIA L40S", memory_gb = 48 }
      "rtx-pro-server-6000" = { architecture = "Blackwell", display_name = "NVIDIA RTX PRO 6000 Server Edition", memory_gb = 96 }
      "t4"                  = { architecture = "Turing", display_name = "NVIDIA T4", memory_gb = 16 }
      "v100"                = { architecture = "Volta", display_name = "NVIDIA V100 32 GB", memory_gb = 32 }
    }
    capacity_types = {
      "on-demand" = { display_name = "On-demand" }
      "spot"      = { display_name = "Spot" }
    }
  }
  gpu_catalog_offers = {
    for pair in setproduct(keys(local.gpu_catalog.models), keys(local.gpu_catalog.capacity_types)) :
    "${pair[0]}-${pair[1]}" => {
      capacity_type         = pair[1]
      capacity_display_name = local.gpu_catalog.capacity_types[pair[1]].display_name
      model                 = pair[0]
      model_architecture    = local.gpu_catalog.models[pair[0]].architecture
      model_display_name    = local.gpu_catalog.models[pair[0]].display_name
      model_memory_gb       = local.gpu_catalog.models[pair[0]].memory_gb
    }
  }
  accelerator_kind_names = {
    gpu = "GPU"
    tpu = "TPU"
  }

  legacy_accelerator_offers = {
    for offer_key, offer in try(local.selected_workspace_placement.gpu_offers, {}) : offer_key => {
      capacity_type = offer.capacity_type
      class         = offer.model
      kind          = "gpu"
      max_count     = offer.max_count
      node_selector = {
        key   = "gpu-class"
        value = offer.model
      }
      resource = "nvidia.com/gpu"
      taint = {
        effect = "NoSchedule"
        key    = "nvidia.com/gpu"
        value  = "present"
      }
      workspace_max = offer.workspace_max
    }
  }
  raw_accelerator_offers = try(
    local.selected_workspace_placement.accelerator_offers,
    local.legacy_accelerator_offers,
  )
  available_accelerator_offers = {
    for offer_key, offer in local.raw_accelerator_offers : offer_key => merge(offer, {
      description = offer.kind == "gpu" ? format(
        "%s, %s GB VRAM. Up to %d per workspace.",
        try(local.gpu_catalog.models[offer.class].architecture, "GPU"),
        try(local.gpu_catalog.models[offer.class].memory_gb, 0),
        offer.max_count,
        ) : format(
        "%s accelerator class. This topology attaches %d chips.",
        upper(offer.class),
        offer.max_count,
      )
      display_name = format(
        "%s - %s",
        offer.kind == "gpu" ? try(local.gpu_catalog.models[offer.class].display_name, upper(offer.class)) : "${local.accelerator_kind_names[offer.kind]} ${offer.class}",
        try(local.gpu_catalog.capacity_types[offer.capacity_type].display_name, title(offer.capacity_type)),
      )
    })
  }

  # LINT.IfChange(no-accelerator-sentinel)
  no_accelerator = "__none__"
  # LINT.ThenChange(//src/infra/definitions/workspaces/templates/dev/parameters.tf:no-accelerator-sentinel)
  selected_accelerator_key = data.coder_parameter.accelerator.value
  selected_accelerator_offer = local.selected_accelerator_key == local.no_accelerator ? null : try(
    local.available_accelerator_offers[local.selected_accelerator_key],
    null,
  )
  accelerator_count_configurable = local.selected_accelerator_offer == null ? false : (
    local.selected_accelerator_offer.kind == "gpu" &&
    local.selected_accelerator_offer.max_count > 1
  )
  accelerator_count = local.selected_accelerator_offer == null ? 0 : (
    local.accelerator_count_configurable ? try(
      tonumber(one(data.coder_parameter.accelerator_count[*].value)),
      -1,
    ) : local.selected_accelerator_offer.max_count
  )
  effective_workspace_cpu = merge(local.workspace_cpu, {
    max = local.selected_accelerator_offer == null ? local.workspace_cpu.max : local.selected_accelerator_offer.workspace_max.cpu
  })
  effective_workspace_memory = merge(local.workspace_memory, {
    max = local.selected_accelerator_offer == null ? local.workspace_memory.max : local.selected_accelerator_offer.workspace_max.memory_gib
  })

  workspace_cpu_burst        = try(tonumber(data.coder_parameter.cpu_burst.value), 0)
  workspace_cpu_limit        = min(local.effective_workspace_cpu.max, try(tonumber(data.coder_parameter.cpu.value), local.effective_workspace_cpu.default) + local.workspace_cpu_burst)
  workspace_memory_burst_gib = try(tonumber(data.coder_parameter.memory_burst_gib.value), 0)
  workspace_memory_limit_gib = min(local.effective_workspace_memory.max, try(tonumber(data.coder_parameter.memory_gib.value), local.effective_workspace_memory.default) + local.workspace_memory_burst_gib)

  # Inlined accelerator placement contracts (previously modules/accelerator_placement)
  selected_offer                = local.selected_accelerator_offer
  accelerator_placement_enabled = local.selected_offer != null && local.accelerator_count > 0

  accelerator_node_selector = local.accelerator_placement_enabled ? {
    "karpenter.sh/capacity-type"             = local.selected_offer.capacity_type
    (local.selected_offer.node_selector.key) = local.selected_offer.node_selector.value
  } : {}

  accelerator_resources = local.accelerator_placement_enabled ? {
    (local.selected_offer.resource) = tostring(local.accelerator_count)
  } : {}

  accelerator_tolerations = local.accelerator_placement_enabled ? [{
    effect             = local.selected_offer.taint.effect
    key                = local.selected_offer.taint.key
    operator           = "Equal"
    toleration_seconds = null
    value              = local.selected_offer.taint.value
  }] : []

  workspace_resilience_tolerations = [
    {
      effect             = "NoExecute"
      key                = "node.kubernetes.io/not-ready"
      operator           = "Exists"
      toleration_seconds = 30
      value              = null
    },
    {
      effect             = "NoExecute"
      key                = "node.kubernetes.io/unreachable"
      operator           = "Exists"
      toleration_seconds = 30
      value              = null
    },
  ]

  pod_tolerations = concat(local.accelerator_tolerations, local.workspace_resilience_tolerations)
}
