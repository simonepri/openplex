# Tests single-writer volume isolation, active pod status parsing, and credential rotation in workspace templates.

mock_provider "coder" {
  mock_data "coder_parameter" {
    defaults = { value = "ray_data" }
  }
}
mock_provider "external" {}
mock_provider "kubernetes" {}

variables {
  access_alias_domain              = "k8s.example.invalid"
  ca_config_map_name               = "cluster-local-ca"
  cell                             = "cell-eaws-lh1"
  control_plane_ca_base64          = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tClkyOXVkSEp2YkMxd2JHRnVaUzFqWVE9PQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg=="
  deployment_domain                = "example.com"
  headscale_url                    = "https://headscale.ctrl-eaws-lh1.k8s.example.invalid"
  repository_url                   = "git://172.19.255.21:9418/cluster-config.git"
  storage_class_name               = "workspace-expandable"
  team                             = "examples"
  workload_registry                = "origin-registry:5000/000000000000/us-east-1"
  workload_registry_insecure       = "false"
  workload_origin_auth_mode        = "floci"
  workload_origin_provider         = "floci"
  workload_origin_region           = "us-east-1"
  workload_origin_role_arn         = ""
  workload_origin_token_audience   = ""
  workload_origin_token_file       = ""
  workspace_arch                   = "arm64"
  workspace_backup_proxy_image     = "registry.invalid/cluster/workspace-backup-proxy@sha256:8a37fbafb559d495b7b07d38f0365d247e32d82bd34bcf1e907b5611ddf0b5c1"
  workspace_image                  = "registry.invalid/workspace@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  workspace_incarnation_inventory  = "{\"cell-eaws-lh1\":\"abcdef123456\"}"
  workspace_namespace              = "team-examples-workspaces"
  workspace_placement_inventory    = "{\"cell-eaws-lh1\":{\"cpu\":{\"default\":1,\"max\":1,\"min\":1},\"gpu_offers\":{},\"memory_gib\":{\"default\":2,\"max\":11,\"min\":1},\"storage_gib\":{\"default\":16,\"max\":32,\"min\":16}}}"
  workspace_service_account        = "coder-workspace"
  workspace_virtual_name_inventory = "{\"cell-eaws-lh1\":\"eaws-lh1\"}"
}

override_data {
  target = data.coder_workspace.me
  values = {
    access_url        = "https://coder.ctrl-eaws-lh1.k8s.unit.test"
    id                = "0780dd84-e91d-4ea2-ad24-5287129f1ed4"
    is_prebuild_claim = false
    name              = "dev"
    start_count       = 1
  }
}

override_data {
  target = data.coder_workspace_owner.me
  values = { name = "ldap" }
}

override_data {
  target = data.coder_parameter.cluster
  values = { value = "cell-eaws-lh1" }
}

override_data {
  target = data.coder_parameter.cpu
  values = { value = "1" }
}

override_data {
  target = data.coder_parameter.cpu_burst
  values = { value = "0" }
}

override_data {
  target = data.coder_parameter.accelerator
  values = { value = "__none__" }
}

override_data {
  target = data.coder_parameter.accelerator_count
  values = { value = "0" }
}

override_data {
  target = data.coder_parameter.home_disk_gib
  values = { value = "16" }
}

override_data {
  target = data.coder_parameter.memory_gib
  values = { value = "8" }
}

override_data {
  target = data.coder_parameter.memory_burst_gib
  values = { value = "0" }
}

override_data {
  target = data.coder_parameter.restore_selector
  values = { value = "__start-fresh__" }
}

override_data {
  target = data.coder_parameter.ssh_enabled
  values = { value = "false" }
}

override_data {
  target = data.coder_parameter.ssh_public_key
  values = { value = "" }
}

override_data {
  target = data.external.attested_owner
  values = {
    result = {
      attested           = "true"
      email              = "ldap@example.invalid"
      id                 = "8fcf5fb5-28b7-4eae-b10c-878b71ca2a8f"
      preferred_username = "ldap"
      preview            = "false"
      principal_id       = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs"
    }
  }
}

override_data {
  target = data.external.workspace_build_context
  values = {
    result = { build_id = "c31cfaca-9746-4dd4-8a5f-215bf5b050cb" }
  }
}

override_data {
  target = data.kubernetes_resources.workspace_cell_ca
  values = {
    objects = [{
      apiVersion = "v1"
      kind       = "ConfigMap"
      metadata = {
        name      = "cluster-local-ca"
        namespace = "team-examples-workspaces"
      }
      data = {
        "ca.crt" = "-----BEGIN CERTIFICATE-----\nY2VsbC1jYQ==\n-----END CERTIFICATE-----\n"
      }
    }]
  }
}

override_data {
  target = data.kubernetes_resources.workspace_origin
  values = {
    objects = [{
      apiVersion = "v1"
      kind       = "ConfigMap"
      metadata = {
        name      = "coder-workspace-origin"
        namespace = "team-examples-workspaces"
      }
      data = {
        authMode       = "floci"
        cell           = "cell-eaws-lh1"
        originRegion   = "us-east-1"
        originRegistry = "origin-registry:5000/000000000000/us-east-1"
        provider       = "floci"
        roleArn        = ""
        tokenAudience  = ""
        tokenFile      = ""
      }
    }]
  }
}

override_resource {
  target          = coder_agent.main
  override_during = plan
  values = {
    id          = "4182d290-ba12-4141-ae88-06073bc29b8b"
    init_script = "BINARY_URL=https://coder.ctrl-eaws-lh1.k8s.example.invalid/bin/coder-linux-arm64\nexport CODER_AGENT_URL=\"https://coder.ctrl-eaws-lh1.k8s.example.invalid/\"\n"
    token       = "fixture-agent-token"
  }
}

run "accepts_kubernetes_empty_list_encodings" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  assert {
    condition     = kubernetes_deployment_v1.workspace[0].metadata[0].namespace == "team-examples-workspaces"
    error_message = "Kubernetes list metadata and items=null must normalize without changing the team dev namespace."
  }

  assert {
    condition = (
      kubernetes_config_map_v1.workspace_ca[0].data["ca.crt"] == join("\n", [
        "-----BEGIN CERTIFICATE-----\nY2VsbC1jYQ==\n-----END CERTIFICATE-----",
        "-----BEGIN CERTIFICATE-----\nY29udHJvbC1wbGFuZS1jYQ==\n-----END CERTIFICATE-----",
      ]) &&
      one([
        for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
        volume.config_map[0].name if volume.name == "workspace-ca"
      ]) == kubernetes_config_map_v1.workspace_ca[0].metadata[0].name
    )
    error_message = "The workspace trust bundle must contain both the selected-cell and control-plane CAs and be mounted by name."
  }

  assert {
    condition = (
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].readiness_probe[0].exec[0].command[2] ==
      "curl --connect-timeout 1 --max-time 1 --noproxy '*' --fail --silent --output /dev/null http://127.0.0.1:2113/debug/manifest"
    )
    error_message = "Workspace Pod readiness must prove that the Coder agent fetched its authenticated manifest."
  }

  assert {
    condition = (
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].metadata[0].labels["com.coder.workspace.build.id"] ==
      data.external.workspace_build_context.result.build_id
    )
    error_message = "The workspace Pod must identify its Coder build so readiness cannot accept an older Pod."
  }

  assert {
    condition = one([
      for container in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].init_container :
      length(container.startup_probe) == 0 && length(container.readiness_probe) == 1 if container.name == "tailnet"
    ])
    error_message = "Tailnet readiness must affect Pod readiness without blocking the workspace agent from starting."
  }

  assert {
    condition = (
      can(regex(
        "(?s)workspace-identity.sh.*workspace-shell.sh \\|\\| exit.*workspace-ssh.sh.*BINARY_URL=",
        kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].command[2],
      )) &&
      kubernetes_config_map_v1.access.data["workspace-shell.sh"] == file("${path.module}/container/init/workspace-shell.sh")
    )
    error_message = "Shell defaults must be mounted and seeded after identity setup, before SSH and Coder can open a terminal."
  }

  assert {
    condition = (
      contains(keys(kubernetes_deployment_v1.workspace[0].metadata[0].labels), format("k8s.%s/workspace-lineage", "example.invalid")) &&
      contains(keys(kubernetes_deployment_v1.workspace[0].spec[0].template[0].metadata[0].labels), format("k8s.%s/workspace-lineage", "example.invalid"))
    )
    error_message = "The Deployment and Pod template must carry the durable lineage under the installation-derived Kubernetes label namespace."
  }

  assert {
    condition = (
      kubernetes_deployment_v1.workspace[0].metadata[0].labels["availability-class"] == "ha" &&
      kubernetes_deployment_v1.workspace[0].metadata[0].labels["latency-class"] == "ls" &&
      kubernetes_deployment_v1.workspace[0].metadata[0].labels["kueue.x-k8s.io/queue-name"] == "ha" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].metadata[0].annotations["kueue.x-k8s.io/pod-suspending-parent"] == "deployment" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].metadata[0].labels["kueue.x-k8s.io/managed"] == "true" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].metadata[0].labels["kueue.x-k8s.io/queue-name"] == "ha" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].priority_class_name == "ha-ls"
    )
    error_message = "The workspace Deployment and Pod template must enter the team's HA Kueue queue without admission-time Terraform drift."
  }

  assert {
    condition     = terraform_data.workspace_scheduling_contract.input == "kueue-deployment-v1"
    error_message = "The Kueue scheduling contract must retain a versioned replacement trigger for existing running workspace Deployments."
  }

  assert {
    condition = one([
      for container in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].init_container :
      container.image if container.name == "backup-proxy"
    ]) == var.workspace_backup_proxy_image
    error_message = "The backup proxy must use the exact administrator-published immutable image reference."
  }

  assert {
    condition = (
      one([
        for mount in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].volume_mount :
        mount if mount.name == "buildbuddy"
      ]).mount_path == "/var/run/workspace/buildbuddy" &&
      one([
        for mount in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].volume_mount :
        mount if mount.name == "buildbuddy"
      ]).read_only &&
      one([
        for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
        volume.projected[0] if volume.name == "buildbuddy"
      ]).default_mode == "0400" &&
      one([
        for source in one([
          for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
          volume.projected[0].sources if volume.name == "buildbuddy"
        ]) : source.config_map[0] if length(source.config_map) == 1
      ]).name == "workspace-buildbuddy-config" &&
      one([
        for source in one([
          for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
          volume.projected[0].sources if volume.name == "buildbuddy"
        ]) : source.secret[0] if length(source.secret) == 1
      ]).name == "workspace-buildbuddy-auth"
    )
    error_message = "The workspace must mount the private endpoints and secret header fragments together through one read-only projected volume."
  }

  assert {
    condition = (
      try(length(kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].node_selector), 0) == 0 &&
      try(length([
        for toleration in try(kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration, []) :
        toleration if !contains(["node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable"], toleration.key)
      ]), 0) == 0 &&
      tostring(try(one([
        for toleration in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration :
        toleration if toleration.key == "node.kubernetes.io/not-ready"
      ]).toleration_seconds, "")) == "30" &&
      tostring(try(one([
        for toleration in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration :
        toleration if toleration.key == "node.kubernetes.io/unreachable"
      ]).toleration_seconds, "")) == "30"
    )
    error_message = "The workspace must let Kueue assign ordinary capacity instead of selecting or tolerating a dedicated node pool."
  }

  assert {
    condition = (
      data.coder_parameter.accelerator.default == "__none__" &&
      data.coder_parameter.accelerator.value == "__none__" &&
      jsondecode(data.coder_parameter.accelerator.styling).disabled &&
      length(data.coder_parameter.accelerator.option) == 1 &&
      one(data.coder_parameter.accelerator.option).name == "None" &&
      length(data.coder_parameter.accelerator_count) == 0 &&
      local.accelerator_count == 0
    )
    error_message = "A CPU-only cluster must render Accelerators set to None without an accelerator count control."
  }

  assert {
    condition = (
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].requests.cpu == "1" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].limits.cpu == "1"
    )
    error_message = "A local workspace must use its selected whole-vCPU request and limit."
  }
}

run "accepts_heterogeneous_kubernetes_pod_metadata_items" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzcifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJwb2QtMSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In0sImRlbGV0aW9uVGltZXN0YW1wIjoiMjAyNi0wOS0xNlQyMDowMDowMFoifX0seyJtZXRhZGF0YSI6eyJuYW1lIjoicG9kLTIiLCJsYWJlbHMiOnsiY29tLmNvZGVyLndvcmtzcGFjZS5pZCI6IjA3ODBkZDg0LWU5MWQtNGVhMi1hZDI0LTUyODcxMjlmMWVkNCIsImNvbS5jb2Rlci53b3Jrc3BhY2UubmFtZSI6ImRldiJ9fX1dfQ=="
      }
    }
  }

  assert {
    condition     = kubernetes_deployment_v1.workspace[0].metadata[0].namespace == "team-examples-workspaces"
    error_message = "Heterogeneous pod metadata items must not cause type unification failures in writer inventory validation."
  }
}

run "accepts_catalog_selector_during_parameter_preview" {
  command = plan

  override_data {
    target = data.coder_parameter.restore_selector
    values = { value = "manifest.with-dots_1" }
  }

  override_data {
    target = data.external.attested_owner
    values = {
      result = {
        attested           = "false"
        email              = "template-import@invalid"
        id                 = "00000000-0000-4000-8000-000000000000"
        preferred_username = "template_import"
        preview            = "true"
        principal_id       = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
      }
    }
  }

  assert {
    condition = (
      local.template_preview &&
      local.restore_requested &&
      local.restore_selector == "manifest.with-dots_1"
    )
    error_message = "Dynamic parameter preview must preserve the catalog selector without querying Kubernetes or requiring cached owner options."
  }
}

run "selects_one_complete_tpu_topology_without_a_count_control" {
  command = plan

  variables {
    workspace_placement_inventory = jsonencode({
      "cell-eaws-lh1" = {
        accelerator_offers = {
          "v5e-2x2-spot" = {
            capacity_type = "spot"
            class         = "v5e-2x2"
            kind          = "tpu"
            max_count     = 4
            node_selector = {
              key   = "tpu-class"
              value = "v5e-2x2"
            }
            resource = "google.com/tpu"
            taint = {
              effect = "NoSchedule"
              key    = "google.com/tpu"
              value  = "present"
            }
            workspace_max = {
              cpu        = 3
              memory_gib = 8
            }
          }
        }
        cpu = {
          default = 1
          max     = 1
          min     = 1
        }
        gpu_offers = {}
        memory_gib = {
          default = 2
          max     = 8
          min     = 1
        }
        storage_gib = {
          default = 16
          max     = 32
          min     = 16
        }
      }
    })
  }

  override_data {
    target = data.coder_parameter.accelerator
    values = { value = "v5e-2x2-spot" }
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  assert {
    condition = (
      length(data.coder_parameter.accelerator_count) == 0 &&
      local.accelerator_count == 4 &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].requests["google.com/tpu"] == "4" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].limits["google.com/tpu"] == "4" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].node_selector["tpu-class"] == "v5e-2x2" &&
      one([
        for toleration in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration :
        toleration if toleration.key == "google.com/tpu"
      ]).key == "google.com/tpu"
    )
    error_message = "A TPU selection must attach its complete topology without exposing a variable count control."
  }
}

run "exposes_the_team_compile_cache" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  assert {
    condition = toset([
      for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
      item.name if startswith(item.name, "TORCHINDUCTOR_")
      ]) == toset([
      "TORCHINDUCTOR_CACHE_DIR",
      "TORCHINDUCTOR_FX_GRAPH_CACHE",
      "TORCHINDUCTOR_FX_GRAPH_REMOTE_CACHE",
      "TORCHINDUCTOR_REDIS_URL",
    ])
    error_message = "A team dev workspace must receive the complete compiler cache contract."
  }

  assert {
    condition = one([
      for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
      item if item.name == "TORCHINDUCTOR_REDIS_URL"
    ]).value_from[0].secret_key_ref[0].name == "torch-compile-cache"
    error_message = "A team dev workspace must read the namespace-local compiler cache endpoint."
  }
}

run "uses_eks_pod_identity_without_a_projected_sts_token" {
  command = plan

  variables {
    ca_config_map_name               = ""
    cell                             = "cell-aws-usw2"
    workload_registry                = "999988887777.dkr.ecr.us-west-2.amazonaws.com"
    workload_registry_insecure       = "false"
    workload_origin_auth_mode        = "eks-pod-identity"
    workload_origin_provider         = "aws"
    workload_origin_region           = "us-west-2"
    workload_origin_role_arn         = "arn:aws:iam::999988887777:role/cluster-cell-aws-examples-workspace-ecr"
    workload_origin_token_audience   = ""
    workload_origin_token_file       = ""
    workspace_incarnation_inventory  = "{\"cell-aws-usw2\":\"aaaaaaaaaaaa\"}"
    workspace_placement_inventory    = "{\"cell-aws-usw2\":{\"cpu\":{\"default\":10,\"max\":190,\"min\":1},\"gpu_offers\":{\"a10g-on-demand\":{\"capacity_type\":\"on-demand\",\"max_count\":8,\"model\":\"a10g\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"a10g-spot\":{\"capacity_type\":\"spot\",\"max_count\":8,\"model\":\"a10g\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"l4-on-demand\":{\"capacity_type\":\"on-demand\",\"max_count\":8,\"model\":\"l4\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"l4-spot\":{\"capacity_type\":\"spot\",\"max_count\":8,\"model\":\"l4\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}}},\"memory_gib\":{\"default\":32,\"max\":1400,\"min\":1},\"storage_gib\":{\"default\":256,\"max\":1024,\"min\":16}}}"
    workspace_virtual_name_inventory = "{\"cell-aws-usw2\":\"aws-usw2\"}"
  }

  override_data {
    target = data.coder_parameter.cluster
    values = { value = "cell-aws-usw2" }
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  override_data {
    target = data.kubernetes_resources.workspace_origin
    values = {
      objects = [{
        metadata = {
          name      = "coder-workspace-origin"
          namespace = "team-examples-workspaces"
        }
        data = {
          authMode       = "eks-pod-identity"
          cell           = "cell-aws-usw2"
          originRegion   = "us-west-2"
          originRegistry = "999988887777.dkr.ecr.us-west-2.amazonaws.com"
          provider       = "aws"
          roleArn        = "arn:aws:iam::999988887777:role/cluster-cell-aws-examples-workspace-ecr"
          tokenAudience  = ""
          tokenFile      = ""
        }
      }]
    }
  }

  assert {
    condition = (
      length([
        for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
        volume if volume.name == "workload-origin-identity"
      ]) == 0 &&
      length([
        for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
        item if item.name == "AWS_ROLE_ARN" || item.name == "AWS_WEB_IDENTITY_TOKEN_FILE"
      ]) == 0
    )
    error_message = "EKS Pod Identity must rely on the cell association and must not receive a projected STS web-identity token."
  }

  assert {
    condition = (
      alltrue([
        for name in [
          "AWS_ACCESS_KEY_ID",
          "AWS_CONTAINER_AUTHORIZATION_TOKEN",
          "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
          "AWS_SECRET_ACCESS_KEY",
          "AWS_SECURITY_TOKEN",
          "AWS_SESSION_TOKEN",
          ] : length([
            for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
            item.value if item.name == name
        ]) == 0
      ]) &&
      one([
        for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
        item.value if item.name == "AWS_PROFILE"
      ]) == "team-s3" &&
      one([
        for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
        item.value if item.name == "AWS_SHARED_CREDENTIALS_FILE"
      ]) == "/var/run/workspace/s3/credentials"
    )
    error_message = "The template must not inject ambient static AWS credentials and must preserve the separately scoped storage profile."
  }
}

run "selects_external_cell_from_multi_cell_inventory" {
  command = plan

  variables {
    ca_config_map_name               = ""
    cell                             = "cell-aws-usw2"
    workload_registry                = "999988887777.dkr.ecr.us-west-2.amazonaws.com"
    workload_registry_insecure       = "false"
    workload_origin_auth_mode        = "web-identity"
    workload_origin_provider         = "gcp"
    workload_origin_region           = "us-west-2"
    workload_origin_role_arn         = "arn:aws:iam::999988887777:role/cluster-cell-gcp-examples-workspace-ecr"
    workload_origin_token_audience   = "sts.amazonaws.com"
    workload_origin_token_file       = "/var/run/secrets/workload-origin/token"
    workspace_incarnation_inventory  = "{\"cell-aws-usw2\":\"aaaaaaaaaaaa\",\"cell-gcp-euw4\":\"cccccccccccc\"}"
    workspace_placement_inventory    = "{\"cell-aws-usw2\":{\"cpu\":{\"default\":10,\"max\":190,\"min\":1},\"gpu_offers\":{\"a10g-on-demand\":{\"capacity_type\":\"on-demand\",\"max_count\":8,\"model\":\"a10g\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"a10g-spot\":{\"capacity_type\":\"spot\",\"max_count\":8,\"model\":\"a10g\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"l4-on-demand\":{\"capacity_type\":\"on-demand\",\"max_count\":8,\"model\":\"l4\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"l4-spot\":{\"capacity_type\":\"spot\",\"max_count\":8,\"model\":\"l4\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}}},\"memory_gib\":{\"default\":32,\"max\":1400,\"min\":1},\"storage_gib\":{\"default\":256,\"max\":1024,\"min\":16}},\"cell-gcp-euw4\":{\"cpu\":{\"default\":4,\"max\":7,\"min\":1},\"gpu_offers\":{\"l4-on-demand\":{\"capacity_type\":\"on-demand\",\"max_count\":1,\"model\":\"l4\",\"workspace_max\":{\"cpu\":7,\"memory_gib\":25}},\"l4-spot\":{\"capacity_type\":\"spot\",\"max_count\":1,\"model\":\"l4\",\"workspace_max\":{\"cpu\":7,\"memory_gib\":25}}},\"memory_gib\":{\"default\":16,\"max\":25,\"min\":1},\"storage_gib\":{\"default\":256,\"max\":1024,\"min\":16}}}"
    workspace_virtual_name_inventory = "{\"cell-aws-usw2\":\"aws-usw2\",\"cell-gcp-euw4\":\"gcp-euw4\"}"
  }

  override_data {
    target = data.coder_parameter.cluster
    values = { value = "cell-gcp-euw4" }
  }

  override_data {
    target = data.coder_parameter.accelerator
    values = { value = "l4-spot" }
  }

  override_data {
    target = data.coder_parameter.cpu
    values = { value = "7" }
  }

  override_data {
    target = data.coder_parameter.cpu_burst
    values = { value = "0" }
  }

  override_data {
    target = data.coder_parameter.memory_gib
    values = { value = "25" }
  }

  override_data {
    target = data.coder_parameter.memory_burst_gib
    values = { value = "0" }
  }


  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  override_data {
    target = data.kubernetes_resources.workspace_origin
    values = {
      objects = [{
        metadata = {
          name      = "coder-workspace-origin"
          namespace = "team-examples-workspaces"
        }
        data = {
          authMode       = "web-identity"
          cell           = "cell-gcp-euw4"
          originRegion   = "us-west-2"
          originRegistry = "999988887777.dkr.ecr.us-west-2.amazonaws.com"
          provider       = "gcp"
          roleArn        = "arn:aws:iam::999988887777:role/cluster-cell-gcp-examples-workspace-ecr"
          tokenAudience  = "sts.amazonaws.com"
          tokenFile      = "/var/run/secrets/workload-origin/token"
        }
      }]
    }
  }

  assert {
    condition = (
      one([
        for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
        item.value if item.name == "AWS_ROLE_ARN"
      ]) == "arn:aws:iam::999988887777:role/cluster-cell-gcp-examples-workspace-ecr" &&
      one([
        for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
        item.value if item.name == "AWS_WEB_IDENTITY_TOKEN_FILE"
      ]) == "/var/run/secrets/workload-origin/token"
    )
    error_message = "An external managed workspace must receive its exact AWS role and projected token path."
  }

  assert {
    condition = (
      one([
        for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
        volume.projected[0] if volume.name == "workload-origin-identity"
      ]).default_mode == "0440" &&
      one(one([
        for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
        volume.projected[0].sources if volume.name == "workload-origin-identity"
      ])).service_account_token[0].audience == "sts.amazonaws.com" &&
      one(one([
        for volume in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].volume :
        volume.projected[0].sources if volume.name == "workload-origin-identity"
      ])).service_account_token[0].path == "token"
    )
    error_message = "An external managed workspace must mount one group-readable sts.amazonaws.com token at the canonical path."
  }

  assert {
    condition = (
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].requests.cpu == "7" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].requests.memory == "25Gi" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].requests["nvidia.com/gpu"] == "1" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].limits["nvidia.com/gpu"] == "1" &&
      jsonencode(kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].node_selector) == jsonencode({
        "karpenter.sh/capacity-type" = "spot"
        "gpu-class"                  = "l4"
      }) &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).effect == "NoSchedule" &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).key == "nvidia.com/gpu" &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).operator == "Equal" &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).value == "present"
    )
    error_message = "A cross-cell GPU workspace must use the selected cell's CPU, memory, exact GPU resource, portable selectors, and native taint toleration."
  }

  assert {
    condition = (
      !jsondecode(data.coder_parameter.accelerator.styling).disabled &&
      toset(data.coder_parameter.accelerator.option[*].value) == toset([
        "__none__",
        "l4-on-demand",
        "l4-spot",
      ]) &&
      data.coder_parameter.accelerator.value == "l4-spot" &&
      length(data.coder_parameter.accelerator_count) == 0 &&
      local.accelerator_count == 1
    )
    error_message = "GCP must expose None and only its available accelerator offers through one dropdown without a redundant fixed count control."
  }

  assert {
    condition = (
      one([
        for item in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].env :
        item.value if item.name == "WORKSPACE_CELL_INCARNATION"
      ]) == "cccccccccccc" &&
      toset([
        for mount in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].volume_mount :
        mount.mount_path if contains(["home", "scratch"], mount.name)
      ]) == toset(["/fs/s3/gcp-euw4/home", "/fs/s3/gcp-euw4/scratch"])
    )
    error_message = "A cross-cell workspace must use the selected cell's immutable incarnation and storage virtual name."
  }

  assert {
    condition = one([
      for item in one([
        for container in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].init_container :
        container if container.name == "backup-proxy"
      ]).env : item.value if item.name == "RCLONE_CONFIG_OWNER_UPSTREAMS"
    ]) == "repository=backend:gcp-euw4/backups/dev/examples/repos/8fcf5fb5-28b7-4eae-b10c-878b71ca2a8f/ claims=backend:gcp-euw4/backups/dev/examples/claims/8fcf5fb5-28b7-4eae-b10c-878b71ca2a8f/"
    error_message = "The backup proxy must address only the selected cell's storage virtual name."
  }
}

run "selects_aws_a100_80gb_spot_through_accelerator_placement" {
  command = plan

  variables {
    ca_config_map_name             = ""
    cell                           = "cell-aws-usw2"
    workload_registry              = "999988887777.dkr.ecr.us-west-2.amazonaws.com"
    workload_registry_insecure     = "false"
    workload_origin_auth_mode      = "eks-pod-identity"
    workload_origin_provider       = "aws"
    workload_origin_region         = "us-west-2"
    workload_origin_role_arn       = "arn:aws:iam::999988887777:role/cluster-cell-aws-examples-workspace-ecr"
    workload_origin_token_audience = ""
    workload_origin_token_file     = ""
    workspace_arch                 = "amd64"
    workspace_incarnation_inventory = jsonencode({
      "cell-aws-usw2" = "aaaaaaaaaaaa"
    })
    workspace_placement_inventory = jsonencode({
      "cell-aws-usw2" = {
        cpu = {
          default = 10
          max     = 190
          min     = 1
        }
        gpu_offers = {
          "a100-80gb-spot" = {
            capacity_type = "spot"
            max_count     = 8
            model         = "a100-80gb"
            workspace_max = {
              cpu        = 94
              memory_gib = 1050
            }
          }
        }
        memory_gib = {
          default = 32
          max     = 1400
          min     = 1
        }
        storage_gib = {
          default = 256
          max     = 1024
          min     = 16
        }
      }
    })
    workspace_virtual_name_inventory = jsonencode({
      "cell-aws-usw2" = "aws-usw2"
    })
  }

  override_data {
    target = data.coder_parameter.cluster
    values = { value = "cell-aws-usw2" }
  }

  override_data {
    target = data.coder_parameter.accelerator
    values = { value = "a100-80gb-spot" }
  }

  override_data {
    target = data.coder_parameter.accelerator_count
    values = { value = "8" }
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  override_data {
    target = data.kubernetes_resources.workspace_origin
    values = {
      objects = [{
        metadata = {
          name      = "coder-workspace-origin"
          namespace = "team-examples-workspaces"
        }
        data = {
          authMode       = "eks-pod-identity"
          cell           = "cell-aws-usw2"
          originRegion   = "us-west-2"
          originRegistry = "999988887777.dkr.ecr.us-west-2.amazonaws.com"
          provider       = "aws"
          roleArn        = "arn:aws:iam::999988887777:role/cluster-cell-aws-examples-workspace-ecr"
          tokenAudience  = ""
          tokenFile      = ""
        }
      }]
    }
  }

  assert {
    condition = (
      length(data.coder_parameter.accelerator_count) == 1 &&
      one(data.coder_parameter.accelerator_count).default == "1" &&
      one(data.coder_parameter.accelerator_count).value == "8" &&
      one(one(data.coder_parameter.accelerator_count).validation).min == 1 &&
      one(one(data.coder_parameter.accelerator_count).validation).max == 8 &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].requests["nvidia.com/gpu"] == "8" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].container[0].resources[0].limits["nvidia.com/gpu"] == "8" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].node_selector["gpu-class"] == "a100-80gb" &&
      kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].node_selector["karpenter.sh/capacity-type"] == "spot" &&
      length(kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].node_selector) == 2 &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).effect == "NoSchedule" &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).key == "nvidia.com/gpu" &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).operator == "Equal" &&
      one([for t in kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].toleration : t if t.key == "nvidia.com/gpu"]).value == "present"
    )
    error_message = "An AWS A100 80 GB Spot selection must enable counts from one through eight and reach the Pod with its portable placement contract."
  }
}

run "rejects_cpu_outside_the_selected_cluster_envelope" {
  command = plan

  override_data {
    target = data.coder_parameter.cpu
    values = { value = "4" }
  }

  override_data {
    target = data.coder_parameter.cpu_burst
    values = { value = "0" }
  }

  override_data {
    target = data.external.attested_owner
    values = {
      result = {
        attested           = "false"
        email              = "ldap@example.invalid"
        id                 = "8fcf5fb5-28b7-4eae-b10c-878b71ca2a8f"
        preferred_username = "ldap"
        preview            = "true"
        principal_id       = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs"
      }
    }
  }

  expect_failures = [coder_agent.main]
}

run "rejects_an_accelerator_unavailable_in_the_selected_cluster" {
  command = plan

  variables {
    cell                             = "cell-aws-usw2"
    workspace_incarnation_inventory  = "{\"cell-aws-usw2\":\"aaaaaaaaaaaa\"}"
    workspace_placement_inventory    = "{\"cell-aws-usw2\":{\"cpu\":{\"default\":10,\"max\":190,\"min\":1},\"gpu_offers\":{\"a10g-on-demand\":{\"capacity_type\":\"on-demand\",\"max_count\":8,\"model\":\"a10g\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"a10g-spot\":{\"capacity_type\":\"spot\",\"max_count\":8,\"model\":\"a10g\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"l4-on-demand\":{\"capacity_type\":\"on-demand\",\"max_count\":8,\"model\":\"l4\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}},\"l4-spot\":{\"capacity_type\":\"spot\",\"max_count\":8,\"model\":\"l4\",\"workspace_max\":{\"cpu\":190,\"memory_gib\":700}}},\"memory_gib\":{\"default\":32,\"max\":1400,\"min\":1},\"storage_gib\":{\"default\":256,\"max\":1024,\"min\":16}}}"
    workspace_virtual_name_inventory = "{\"cell-aws-usw2\":\"aws-usw2\"}"
  }

  override_data {
    target = data.coder_parameter.cluster
    values = { value = "cell-aws-usw2" }
  }

  override_data {
    target = data.coder_parameter.accelerator
    values = { value = "unavailable-spot" }
  }

  override_data {
    target = data.external.attested_owner
    values = {
      result = {
        attested           = "false"
        email              = "ldap@example.invalid"
        id                 = "8fcf5fb5-28b7-4eae-b10c-878b71ca2a8f"
        preferred_username = "ldap"
        preview            = "true"
        principal_id       = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs"
      }
    }
  }

  expect_failures = [coder_agent.main]
}

run "drops_a_stale_accelerator_count_for_none" {
  command = plan

  override_data {
    target = data.coder_parameter.accelerator_count
    values = { value = "100" }
  }

  override_data {
    target = data.external.attested_owner
    values = {
      result = {
        attested           = "false"
        email              = "ldap@example.invalid"
        id                 = "8fcf5fb5-28b7-4eae-b10c-878b71ca2a8f"
        preferred_username = "ldap"
        preview            = "true"
        principal_id       = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs"
      }
    }
  }

  assert {
    condition = (
      length(data.coder_parameter.accelerator_count) == 0 &&
      local.accelerator_count == 0 &&
      length(local.accelerator_resources) == 0
    )
    error_message = "None must remove the count parameter and force zero accelerator resources even when a stale count value is supplied."
  }
}

run "rejects_a_missing_argo_workspace_origin_contract" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  override_data {
    target = data.kubernetes_resources.workspace_origin
    values = { objects = [] }
  }

  expect_failures = [coder_agent.main]
}

run "rejects_a_workspace_namespace_owned_by_another_team" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  variables {
    workspace_namespace = "team-other-workspaces"
  }

  expect_failures = [coder_agent.main]
}

run "rejects_non_list_items" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzYifSwiaXRlbXMiOnt9fQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzYifSwiaXRlbXMiOltdfQ=="
      }
    }
  }

  expect_failures = [kubernetes_deployment_v1.workspace]
}

run "rejects_a_workspace_origin_for_another_cell" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  override_data {
    target = data.kubernetes_resources.workspace_origin
    values = {
      objects = [{
        metadata = {
          name      = "coder-workspace-origin"
          namespace = "team-examples-workspaces"
        }
        data = {
          authMode       = "floci"
          cell           = "cell-other"
          originRegion   = "us-east-1"
          originRegistry = "origin-registry:5000/000000000000/us-east-1"
          provider       = "floci"
          roleArn        = ""
          tokenAudience  = ""
          tokenFile      = ""
        }
      }]
    }
  }

  expect_failures = [coder_agent.main]
}

run "rejects_an_arbitrary_runtime_origin" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  override_data {
    target = data.kubernetes_resources.workspace_origin
    values = {
      objects = [{
        metadata = {
          name      = "coder-workspace-origin"
          namespace = "team-examples-workspaces"
        }
        data = {
          authMode       = "floci"
          cell           = "cell-eaws-lh1"
          originRegion   = "us-east-1"
          originRegistry = "registry.invalid/cluster"
          provider       = "floci"
          roleArn        = ""
          tokenAudience  = ""
          tokenFile      = ""
        }
      }]
    }
  }

  expect_failures = [coder_agent.main]
}

run "retains_the_pod_template_within_one_build" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIml0ZW1zIjpbXX0="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIml0ZW1zIjpbXX0="
      }
    }
  }
}

run "rotates_runtime_credentials_for_a_new_build" {
  command = plan

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIml0ZW1zIjpbXX0="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIml0ZW1zIjpbXX0="
      }
    }
  }

  override_data {
    target = data.external.workspace_build_context
    values = {
      result = { build_id = "a83e553e-3ac8-4425-a376-b15c98555e29" }
    }
  }

  override_resource {
    target = coder_agent.main
    values = {
      id          = "4182d290-ba12-4141-ae88-06073bc29b8b"
      init_script = "BINARY_URL=https://coder.ctrl-eaws-lh1.k8s.example.invalid/bin/coder-linux-arm64\nexport CODER_AGENT_URL=\"https://coder.ctrl-eaws-lh1.k8s.example.invalid/\"\n"
      token       = "fixture-rotated-agent-token"
    }
  }
}

run "rejects_repository_urls_with_embedded_credentials" {
  command = plan

  variables {
    repository_url = "https://oauth-token@github.com/example/private.git"
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  expect_failures = [var.repository_url]
}

run "forks_canonical_lineage_and_scopes_pvc_on_historical_rewind" {
  command = plan

  override_data {
    target = data.coder_parameter.restore_selector
    values = { value = "manifest.with-dots_1" }
  }

  override_data {
    target = data.external.attested_owner
    values = {
      result = {
        attested           = "false"
        email              = "template-import@invalid"
        id                 = "00000000-0000-4000-8000-000000000000"
        preferred_username = "template_import"
        preview            = "true"
        principal_id       = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
      }
    }
  }

  override_data {
    target = data.external.workspace_build_context
    values = {
      result = {
        build_id  = "d42cfaca-9746-4dd4-8a5f-215bf5b050cb"
        timestamp = "1700000099"
      }
    }
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIml0ZW1zIjpbXX0="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIml0ZW1zIjpbXX0="
      }
    }
  }

  override_resource {
    target          = module.coder_snapshots.terraform_data.applied_restore_selector
    override_during = plan
    values = {
      output = ""
    }
  }

  override_resource {
    target          = module.coder_snapshots.terraform_data.applied_disk_gib
    override_during = plan
    values = {
      output = 16
    }
  }

  override_resource {
    target          = module.coder_snapshots.terraform_data.disk_generation
    override_during = plan
    values = {
      output = "0"
    }
  }

  override_resource {
    target          = coder_agent.main
    override_during = plan
    values = {
      id          = "4182d290-ba12-4141-ae88-06073bc29b8b"
      init_script = "BINARY_URL=https://coder.ctrl-eaws-lh1.k8s.example.invalid/bin/coder-linux-arm64\nexport CODER_AGENT_URL=\"https://coder.ctrl-eaws-lh1.k8s.example.invalid/\"\n"
      token       = "fixture-agent-token"
    }
  }

  assert {
    condition = (
      local.is_new_restore &&
      !local.workspace_is_root &&
      local.workspace_lineage == "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1700000099" &&
      local.workspace_parent_snapshot == "manifest.with-dots_1" &&
      module.coder_snapshots.home_volume_claim_name == "coder-0780dd84-e91d-4ea2-ad24-5287129f1ed4-home"
    )
    error_message = "Historical rewind must fork into a new lineage, set parent ancestry, and retain stable home claim."
  }
}

run "derives_readable_machine_for_hyphenated_workspace_name" {
  command = plan

  override_data {
    target = data.coder_workspace.me
    values = {
      access_url        = "https://coder.ctrl-eaws-lh1.k8s.unit.test"
      id                = "0780dd84-e91d-4ea2-ad24-5287129f1ed4"
      is_prebuild_claim = false
      name              = "tomato-clam-80"
      start_count       = 1
    }
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoidG9tYXRvLWNsYW0tODAifX0sInNwZWMiOnsicmVwbGljYXMiOjF9fV19"
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  assert {
    condition     = kubernetes_deployment_v1.workspace[0].spec[0].template[0].spec[0].hostname == "ldap-tomato-clam-80"
    error_message = "A hyphenated workspace name must derive a human-readable machine hostname."
  }
}

run "accepts_empty_headscale_url" {
  command = plan

  variables {
    headscale_url = ""
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  assert {
    condition     = local.active_ctrl_name == ""
    error_message = "Empty headscale_url must produce an empty active_ctrl_name."
  }
}

run "accepts_tailscale_saas_headscale_url" {
  command = plan

  variables {
    headscale_url = "https://login.tailscale.com"
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  assert {
    condition     = local.active_ctrl_name == ""
    error_message = "Tailscale SaaS headscale_url must produce an empty active_ctrl_name."
  }
}

run "rejects_invalid_headscale_url" {
  command = plan

  variables {
    headscale_url = "not-a-valid-url"
  }

  override_data {
    target = data.external.workspace_writer_inventory
    values = {
      result = {
        deployments = "eyJhcGlWZXJzaW9uIjoiYXBwcy92MSIsImtpbmQiOiJEZXBsb3ltZW50TGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOlt7Im1ldGFkYXRhIjp7Im5hbWUiOiJjdXJyZW50LXdvcmtzcGFjZSIsImxhYmVscyI6eyJjb20uY29kZXIud29ya3NwYWNlLmlkIjoiMDc4MGRkODQtZTkxZC00ZWEyLWFkMjQtNTI4NzEyOWYxZWQ0IiwiY29tLmNvZGVyLndvcmtzcGFjZS5uYW1lIjoiZGV2In19LCJzcGVjIjp7InJlcGxpY2FzIjoxfX1dfQ=="
        pods        = "eyJhcGlWZXJzaW9uIjoibWV0YS5rOHMuaW8vdjEiLCJraW5kIjoiUGFydGlhbE9iamVjdE1ldGFkYXRhTGlzdCIsIm1ldGFkYXRhIjp7InJlc291cmNlVmVyc2lvbiI6IjIyNTcwMzUifSwiaXRlbXMiOm51bGx9"
      }
    }
  }

  expect_failures = [var.headscale_url]
}

