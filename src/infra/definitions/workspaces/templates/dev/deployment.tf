# Defines the Kubernetes Deployment resource running the Coder developer workspace pod and associated sidecars.

resource "terraform_data" "workspace_scheduling_contract" {
  # Changing this value replaces the running Deployment before a new Kueue
  # queue label is applied. Kueue rejects that label change in place.
  input = "kueue-deployment-v1"
}

resource "kubernetes_deployment_v1" "workspace" {
  count            = local.workspace_start_count
  wait_for_rollout = false

  metadata {
    name      = "coder-${data.coder_workspace.me.id}"
    namespace = local.workspace_namespace
    labels = merge(local.app_labels, local.workspace_scheduling_labels, {
      (local.workspace_lineage_label) = local.workspace_lineage
      vpa                             = "false"
    })
    annotations = {
      "com.coder.user.email" = local.owner_email
    }
  }

  spec {
    replicas = 1

    selector {
      match_labels = local.app_labels
    }

    strategy {
      type = "Recreate"
    }

    template {
      metadata {
        annotations = {
          # Secret-backed environment variables are read only at container start.
          "com.coder.workspace.build.id"         = data.external.workspace_build_context.result.build_id
          "kueue.x-k8s.io/pod-suspending-parent" = "deployment"
        }
        labels = merge(local.app_labels, local.workspace_pod_scheduling_labels, {
          "com.coder.workspace.build.id"  = data.external.workspace_build_context.result.build_id
          (local.workspace_lineage_label) = local.workspace_lineage
        })
      }

      spec {
        automount_service_account_token  = false
        hostname                         = local.workspace_machine
        priority_class_name              = "ha-ls"
        share_process_namespace          = false
        termination_grace_period_seconds = 120
        service_account_name             = var.workspace_service_account
        node_selector                    = local.accelerator_node_selector

        dynamic "toleration" {
          for_each = local.pod_tolerations
          content {
            effect             = toleration.value.effect
            key                = toleration.value.key
            operator           = toleration.value.operator
            toleration_seconds = toleration.value.toleration_seconds
            value              = toleration.value.value
          }
        }

        security_context {
          fs_group               = 1000
          fs_group_change_policy = "OnRootMismatch"
          run_as_group           = 1000
          run_as_non_root        = true
          run_as_user            = 1000
          seccomp_profile { type = "RuntimeDefault" }
        }

        init_container {
          name              = "prepare-workspace-volume"
          image             = var.workspace_image
          image_pull_policy = "IfNotPresent"
          command           = ["sh", "-c", "mkdir -p /workspace-volume/home /workspace-volume/local /workspace-volume/repo"]

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_group               = 1000
            run_as_non_root            = true
            run_as_user                = 1000
            capabilities { drop = ["ALL"] }
          }

          volume_mount {
            mount_path = "/workspace-volume"
            name       = "workspace"
            read_only  = false
          }
        }

        container {
          name              = "workspace"
          image             = var.workspace_image
          image_pull_policy = "IfNotPresent"
          command = [
            "sh",
            "-c",
            "WORKSPACE_BOOT_TOKEN=\"$(od -An -N32 -tx1 /dev/urandom | tr -d '[:space:]')\"\n[ \"$${#WORKSPACE_BOOT_TOKEN}\" -eq 64 ] || exit 1\nexport WORKSPACE_BOOT_TOKEN\nif [ -r /etc/workspace/ca/ca.crt ]; then cat /etc/ssl/certs/ca-certificates.crt /etc/workspace/ca/ca.crt > /tmp/workspace-ca-bundle.crt; fi\n/etc/workspace/access/workspace-identity.sh \"$WORKSPACE_USERNAME\" /tmp/workspace-identity\nexport LD_PRELOAD=\"$(find /usr/lib -name libnss_wrapper.so -print -quit)\" NSS_WRAPPER_PASSWD=/tmp/workspace-identity/passwd NSS_WRAPPER_GROUP=/tmp/workspace-identity/group USER=\"$WORKSPACE_USERNAME\" LOGNAME=\"$WORKSPACE_USERNAME\"\n/etc/workspace/access/workspace-shell.sh || exit $?\n/etc/workspace/access/workspace-ssh.sh /tmp/workspace-identity /tmp/workspace-restore-ready &\n${local.coder_agent_init_script}\nexit 1",
          ]

          env {
            name  = "AWS_SHARED_CREDENTIALS_FILE"
            value = "/var/run/workspace/s3/credentials"
          }
          env {
            name  = "WORKSPACE_S3_TEAMS"
            value = join(",", local.workspace_s3_teams)
          }
          env {
            name  = "AWS_REQUEST_CHECKSUM_CALCULATION"
            value = "when_required"
          }
          env {
            name  = "AWS_RESPONSE_CHECKSUM_VALIDATION"
            value = "when_required"
          }
          env {
            name = "CODER_AGENT_TOKEN"
            value_from {
              secret_key_ref {
                key  = "coder_agent_token"
                name = local.runtime_secret_name
              }
            }
          }
          env {
            name  = "CODER_AGENT_DEVCONTAINERS_ENABLE"
            value = "false"
          }
          env {
            name  = "CODER_CLIENT_TLS_CA_FILE"
            value = "/tmp/workspace-ca-bundle.crt"
          }
          env {
            name = "CODER_SESSION_TOKEN"
            value_from {
              secret_key_ref {
                key  = "coder_session_token"
                name = local.runtime_secret_name
              }
            }
          }
          env {
            name  = "CODER_URL"
            value = local.coder_agent_url
          }
          env {
            name  = "CODER_WORKSPACE_NAME"
            value = data.coder_workspace.me.name
          }
          env {
            name  = "KUBECONFIG"
            value = "/etc/workspace/kubernetes/kubeconfig"
          }
          env {
            name  = "XDG_RUNTIME_DIR"
            value = "/home/coder/.runtime"
          }
          env {
            name = "KOPIA_REPOSITORY_ACCESS_KEY_ID"
            value_from {
              secret_key_ref {
                key  = "backup_proxy_access_key"
                name = local.runtime_secret_name
              }
            }
          }
          env {
            name = "KOPIA_REPOSITORY_SECRET_ACCESS_KEY"
            value_from {
              secret_key_ref {
                key  = "backup_proxy_secret_key"
                name = local.runtime_secret_name
              }
            }
          }
          env {
            name  = "MISE_GLOBAL_CONFIG_FILE"
            value = "/etc/workspace/config/config.toml"
          }
          env {
            name  = "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER"
            value = "/etc/workspace/config/zig-cc.sh"
          }
          env {
            name  = "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER"
            value = "/etc/workspace/config/zig-cc.sh"
          }
          env {
            name  = "WORKSPACE_CELL"
            value = local.selected_cell
          }
          env {
            name  = "WORKSPACE_CHECKOUT_PATH"
            value = var.checkout_path
          }
          env {
            name  = "WORKSPACE_CELL_INCARNATION"
            value = local.selected_incarnation
          }
          env {
            name  = "WORKSPACE_MACHINE"
            value = local.workspace_machine
          }
          env {
            name  = "PASEO_APP_HOSTNAME"
            value = local.paseo_app_hostname
          }
          env {
            name = "ZASPER_ACCESS_TOKEN"
            value_from {
              secret_key_ref {
                key  = "zasper_access_token"
                name = local.runtime_secret_name
              }
            }
          }
          env {
            name  = "SSH_URI"
            value = local.ssh_uri
          }
          env {
            name  = "VSCODE_SSH_URI"
            value = local.vscode_remote_ssh_url
          }
          env {
            name  = "WORKSPACE_NAME"
            value = data.coder_workspace.me.name
          }
          env {
            name  = "WORKSPACE_REPOSITORY_URL"
            value = var.repository_url
          }
          dynamic "env" {
            for_each = module.coder_snapshots.environment_variables
            content {
              name  = env.key
              value = env.value
            }
          }
          env {
            name  = "KOPIA_PASSWORD_FILE"
            value = "/var/run/workspace/snapshot-repository/password"
          }
          env {
            name  = "WORKSPACE_IS_ROOT"
            value = tostring(local.workspace_is_root)
          }
          env {
            name  = "WORKSPACE_PARENT_SNAPSHOT"
            value = local.workspace_parent_snapshot
          }
          env {
            name  = "WORKSPACE_USERNAME"
            value = local.owner_username
          }
          env {
            name  = "SHELL"
            value = "/usr/bin/zsh"
          }
          dynamic "env" {
            for_each = local.workload_origin_environment
            content {
              name  = env.key
              value = env.value
            }
          }
          dynamic "env" {
            for_each = local.torchinductor_environment
            content {
              name  = env.key
              value = env.value
            }
          }
          dynamic "env" {
            for_each = local.workspace_cache_environment
            content {
              name  = env.key
              value = env.value
            }
          }
          dynamic "env" {
            for_each = ["torch-compile-cache"]
            content {
              name = "TORCHINDUCTOR_REDIS_URL"
              value_from {
                secret_key_ref {
                  key      = "redis-url"
                  name     = env.value
                  optional = true
                }
              }
            }
          }
          env {
            name  = "SSH_ACCESS"
            value = local.ssh_enabled ? "enable" : "disable"
          }
          env {
            name  = "SSH_PUBLIC_KEY"
            value = data.coder_parameter.ssh_public_key.value
          }
          env {
            name  = "SSL_CERT_FILE"
            value = "/tmp/workspace-ca-bundle.crt"
          }

          resources {
            requests = merge({
              cpu    = data.coder_parameter.cpu.value
              memory = "${data.coder_parameter.memory_gib.value}Gi"
            }, local.accelerator_resources)
            limits = merge({
              cpu    = tostring(local.workspace_cpu_limit)
              memory = "${local.workspace_memory_limit_gib}Gi"
            }, local.accelerator_resources)
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_group               = 1000
            run_as_non_root            = true
            run_as_user                = 1000
            capabilities { drop = ["ALL"] }
          }

          readiness_probe {
            exec {
              command = [
                "sh",
                "-c",
                "curl --connect-timeout 1 --max-time 1 --noproxy '*' --fail --silent --output /dev/null http://127.0.0.1:2113/debug/manifest",
              ]
            }
            initial_delay_seconds = 1
            period_seconds        = 1
            timeout_seconds       = 2
            failure_threshold     = 3
          }

          volume_mount {
            mount_path = var.checkout_path
            name       = "workspace"
            read_only  = false
            sub_path   = "repo"
          }
          dynamic "volume_mount" {
            for_each = var.checkout_path != "/fs/depot" ? ["/fs/depot"] : []
            content {
              mount_path = volume_mount.value
              name       = "workspace"
              read_only  = false
              sub_path   = "repo"
            }
          }
          volume_mount {
            mount_path = "/etc/workspace/config"
            name       = "mise-config"
            read_only  = true
          }
          volume_mount {
            mount_path = "/etc/zsh/zshrc"
            name       = "mise-config"
            read_only  = true
            sub_path   = "workspace.zshrc"
          }
          volume_mount {
            mount_path = "/etc/workspace/kubernetes"
            name       = "workspace-kubeconfig"
            read_only  = true
          }
          volume_mount {
            mount_path = "/etc/workspace/access"
            name       = "access-config"
            read_only  = true
          }
          volume_mount {
            mount_path = "/usr/local/bin/reboot"
            name       = "access-config"
            sub_path   = "reboot.sh"
            read_only  = true
          }
          volume_mount {
            mount_path = "/usr/local/bin/s3i"
            name       = "access-config"
            sub_path   = "s3i-cli.sh"
            read_only  = true
          }
          volume_mount {
            mount_path = "/home/coder"
            name       = "workspace"
            read_only  = false
            sub_path   = "home"
          }
          volume_mount {
            mount_path = "/tmp"
            name       = "tmp"
            read_only  = false
          }
          volume_mount {
            mount_path = "/var/run/workspace/buildbuddy"
            name       = "buildbuddy"
            read_only  = true
          }
          volume_mount {
            mount_path = "/var/run/workspace/s3"
            name       = "workspace-s3-credentials"
            read_only  = true
          }
          volume_mount {
            mount_path = "/var/run/workspace/snapshot-repository"
            name       = "snapshot-repository"
            read_only  = true
          }
          volume_mount {
            mount_path = "/var/run/workspace/tailnet"
            name       = "tailnet-state"
            read_only  = true
          }
          dynamic "volume_mount" {
            for_each = local.workload_origin_auth_mode == "web-identity" ? [local.workload_origin_token_file] : []
            content {
              mount_path = dirname(volume_mount.value)
              name       = "workload-origin-identity"
              read_only  = true
            }
          }
          volume_mount {
            mount_path = "/var/lib/workspace"
            name       = "workspace"
            read_only  = false
          }
          volume_mount {
            mount_path = "/fs"
            name       = "fs"
            read_only  = false
          }
          volume_mount {
            mount_path = "/fs/local"
            name       = "workspace"
            read_only  = false
            sub_path   = "local"
          }
          volume_mount {
            mount_path = "/fs/s3/${local.selected_virtual_name}/home/legacy"
            name       = "legacy"
            read_only  = false
          }
          volume_mount {
            mount_path = "/fs/s3/aws-use1/home/legacy"
            name       = "legacy-use1"
            read_only  = false
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_teams
            content {
              mount_path = "/fs/s3/${local.selected_virtual_name}/home/${volume_mount.value}"
              name       = "home-${volume_mount.value}"
              read_only  = false
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_teams
            content {
              mount_path = "/fs/s3/${local.selected_virtual_name}/scratch/${volume_mount.value}"
              name       = "scratch-${volume_mount.value}"
              read_only  = false
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_global_teams
            content {
              mount_path = "/fs/s3/global/home/${volume_mount.value}"
              name       = "global-home-${volume_mount.value}"
              read_only  = false
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_global_teams
            content {
              mount_path = "/fs/s3/global/scratch/${volume_mount.value}"
              name       = "global-scratch-${volume_mount.value}"
              read_only  = false
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_global_teams
            content {
              mount_path = "/fs/s3/global/meta/${volume_mount.value}"
              name       = "global-meta-${volume_mount.value}"
              read_only  = false
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_reader_teams
            content {
              mount_path = "/fs/s3/${local.selected_virtual_name}/home/${volume_mount.value}"
              name       = "home-${volume_mount.value}"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_reader_teams
            content {
              mount_path = "/fs/s3/${local.selected_virtual_name}/scratch/${volume_mount.value}"
              name       = "scratch-${volume_mount.value}"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_reader_global_teams
            content {
              mount_path = "/fs/s3/global/home/${volume_mount.value}"
              name       = "global-home-${volume_mount.value}"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_reader_global_teams
            content {
              mount_path = "/fs/s3/global/scratch/${volume_mount.value}"
              name       = "global-scratch-${volume_mount.value}"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = local.workspace_s3_reader_global_teams
            content {
              mount_path = "/fs/s3/global/meta/${volume_mount.value}"
              name       = "global-meta-${volume_mount.value}"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = length(local.workspace_s3_teams) > 0 ? ["meta"] : []
            content {
              mount_path = "/fs/s3/${local.selected_virtual_name}/meta"
              name       = "meta"
              read_only  = true
            }
          }
          volume_mount {
            mount_path = "/etc/workspace/ca"
            name       = "workspace-ca"
            read_only  = true
          }
        }

        init_container {
          name              = "tailnet"
          image             = "ghcr.io/tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065"
          image_pull_policy = "IfNotPresent"
          restart_policy    = "Always"
          command           = ["/bin/sh", "/etc/workspace/access/workspace-tailnet.sh"]

          env {
            name = "CODER_AGENT_TOKEN"
            value_from {
              secret_key_ref {
                key  = "coder_agent_token"
                name = local.runtime_secret_name
              }
            }
          }
          env {
            name  = "HEADSCALE_URL"
            value = var.headscale_url
          }
          env {
            name  = "WORKSPACE_CELL"
            value = local.selected_cell
          }
          env {
            name  = "WORKSPACE_MACHINE"
            value = local.workspace_machine
          }
          env {
            name  = "SSH_ACCESS"
            value = local.ssh_enabled ? "enable" : "disable"
          }

          env {
            name  = "SSL_CERT_FILE"
            value = "/etc/workspace/ca/ca.crt"
          }

          resources {
            requests = local.workspace_sidecar_requests.tailnet
            limits   = { cpu = "250m", memory = "128Mi" }
          }

          readiness_probe {
            exec {
              command = ["test", "-f", "/var/run/workspace/tailnet/workspace-tailnet-ready"]
            }
            initial_delay_seconds = 1
            period_seconds        = 1
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_group               = 1000
            run_as_non_root            = true
            run_as_user                = 1000
            capabilities { drop = ["ALL"] }
          }

          volume_mount {
            mount_path = "/etc/workspace/access"
            name       = "access-config"
            read_only  = true
          }
          volume_mount {
            mount_path = "/tmp"
            name       = "tailnet-tmp"
            read_only  = false
          }
          volume_mount {
            mount_path = "/var/run/workspace/tailnet"
            name       = "tailnet-state"
            read_only  = false
          }
          volume_mount {
            mount_path = "/etc/workspace/ca"
            name       = "workspace-ca"
            read_only  = true
          }
        }

        init_container {
          name = "backup-proxy"
          # LINT.IfChange(workspace-backup-proxy-runtime-image)
          image = var.workspace_backup_proxy_image
          # LINT.ThenChange(//src/infra/definitions/workspaces/templates/dev/variables.tf:workspace-backup-proxy-template-image)
          image_pull_policy = "IfNotPresent"
          restart_policy    = "Always"
          command           = ["/usr/local/bin/rclone"]
          args = [
            "serve",
            "s3",
            "--addr",
            "127.0.0.1:19847",
            "--disable=PutStream",
            "--force-path-style",
            # The gateway passes KMS ETags through as MD5s, so skip the VFS check after full reads.
            "--no-checksum",
            "--vfs-cache-mode",
            "off",
            "owner:",
          ]

          env {
            name  = "RCLONE_CONFIG"
            value = "/tmp/rclone.conf"
          }
          env {
            name  = "RCLONE_CONFIG_BACKEND_TYPE"
            value = "s3"
          }
          env {
            name  = "RCLONE_CONFIG_BACKEND_PROVIDER"
            value = "Rclone"
          }
          env {
            name  = "RCLONE_CONFIG_BACKEND_ENDPOINT"
            value = "http://s3-gateway.s3-system.svc"
          }
          env {
            name  = "RCLONE_CONFIG_BACKEND_FORCE_PATH_STYLE"
            value = "true"
          }
          env {
            name  = "RCLONE_CONFIG_BACKEND_USE_MULTIPART_UPLOADS"
            value = "false"
          }
          # SSE-KMS object ETags are not MD5 digests, so rclone's upload check rejects every write.
          env {
            name  = "RCLONE_IGNORE_CHECKSUM"
            value = "true"
          }
          # LINT.IfChange(dev-workspace-secret-projection)
          env {
            name = "RCLONE_CONFIG_BACKEND_ACCESS_KEY_ID"
            value_from {
              secret_key_ref {
                key  = "access_key_id"
                name = "workspace-backups"
              }
            }
          }
          env {
            name = "RCLONE_CONFIG_BACKEND_SECRET_ACCESS_KEY"
            value_from {
              secret_key_ref {
                key  = "secret_access_key"
                name = "workspace-backups"
              }
            }
          }
          # LINT.ThenChange(//src/infra/argocd/components/coder_workspaces/helm/templates/workspace-backups.yaml:dev-workspace-secret-projection,//src/infra/argocd/components/kyverno/kustomize/dev-secrets-policy.yaml:dev-workspace-secret-projection)
          env {
            name  = "RCLONE_CONFIG_OWNER_TYPE"
            value = "combine"
          }
          env {
            name  = "RCLONE_CONFIG_OWNER_UPSTREAMS"
            value = "repository=backend:${local.selected_virtual_name}/backups/dev/users/${local.owner_id}/repos/ claims=backend:${local.selected_virtual_name}/backups/dev/users/${local.owner_id}/claims/${local.is_cross_cell_restore ? " source=backend:${local.restore_source_virtual_name}/backups/dev/users/${local.owner_id}/repos/" : ""}"
          }
          env {
            name = "RCLONE_AUTH_KEY"
            value_from {
              secret_key_ref {
                key  = "backup_proxy_auth_key"
                name = local.runtime_secret_name
              }
            }
          }
          env {
            name  = "SSL_CERT_FILE"
            value = "/etc/workspace/ca/ca.crt"
          }

          resources {
            requests = local.workspace_sidecar_requests.backup_proxy
            limits   = { cpu = "500m", memory = "256Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_group               = 1000
            run_as_non_root            = true
            run_as_user                = 1000
            capabilities { drop = ["ALL"] }
          }

          volume_mount {
            mount_path = "/tmp"
            name       = "backup-proxy-tmp"
            read_only  = false
          }
          volume_mount {
            mount_path = "/etc/workspace/ca"
            name       = "workspace-ca"
            read_only  = true
          }
        }

        volume {
          name = "access-config"
          config_map {
            default_mode = "0555"
            name         = kubernetes_config_map_v1.access.metadata[0].name
          }
        }

        volume {
          name = "buildbuddy"
          projected {
            default_mode = "0400"
            sources {
              config_map {
                name = "workspace-buildbuddy-config"
                items {
                  key  = "buildbuddy.bazelrc"
                  path = "buildbuddy.bazelrc"
                }
              }
            }
            sources {
              secret {
                name     = "workspace-buildbuddy-auth"
                optional = true
                items {
                  key  = "credentials.bazelrc"
                  path = "credentials.bazelrc"
                }
              }
            }
          }
        }

        volume {
          name = "workspace-kubeconfig"
          config_map {
            default_mode = "0444"
            name         = kubernetes_config_map_v1.workspace_kubeconfig.metadata[0].name
          }
        }

        dynamic "volume" {
          for_each = local.workload_origin_auth_mode == "web-identity" ? [local.workload_origin_token_audience] : []
          content {
            name = "workload-origin-identity"
            projected {
              default_mode = "0440"
              sources {
                service_account_token {
                  audience           = volume.value
                  expiration_seconds = 3600
                  path               = basename(local.workload_origin_token_file)
                }
              }
            }
          }
        }
        volume {
          name = "mise-config"
          config_map {
            default_mode = "0444"
            name         = kubernetes_config_map_v1.mise.metadata[0].name
            items {
              key  = "config.toml"
              mode = "0444"
              path = "config.toml"
            }
            items {
              key  = "workspace-herdr.toml"
              mode = "0444"
              path = "workspace-herdr.toml"
            }
            items {
              key  = "workspace-zellij.kdl"
              mode = "0444"
              path = "workspace-zellij.kdl"
            }
            items {
              key  = "workspace.zsh_plugins.txt"
              mode = "0444"
              path = "workspace.zsh_plugins.txt"
            }
            items {
              key  = "workspace-snazzy.zsh"
              mode = "0444"
              path = "workspace-snazzy.zsh"
            }
            items {
              key  = "workspace.zshrc"
              mode = "0444"
              path = "workspace.zshrc"
            }
            items {
              key  = "zig-cc.sh"
              mode = "0555"
              path = "zig-cc.sh"
            }
            items {
              key  = "kopia-restore.sh"
              mode = "0555"
              path = "kopia-restore.sh"
            }
          }
        }
        volume {
          name = "snapshot-repository"
          secret {
            default_mode = "0400"
            secret_name  = local.snapshot_repository_secret_name
          }
        }
        # LINT.IfChange(workspace-s3-credential-contract)
        volume {
          name = "workspace-s3-credentials"
          secret {
            default_mode = "0400"
            secret_name  = local.workspace_s3_secret_name
            items {
              key  = "credentials"
              path = "credentials"
            }
          }
        }
        volume {
          name = "legacy"
          csi {
            driver = "rclone.csi.veloxpack.io"
            volume_attributes = {
              gid              = "1000"
              "no-checksum"    = "true"
              remote           = "legacy"
              remotePath       = ""
              tpslimit         = "25"
              "tpslimit-burst" = "10"
              uid              = "1000"
              umask            = "0022"
              "vfs-cache-mode" = "off"
            }
            node_publish_secret_ref { name = local.workspace_s3_secret_name }
          }
        }
        volume {
          name = "legacy-use1"
          csi {
            driver = "rclone.csi.veloxpack.io"
            volume_attributes = {
              gid              = "1000"
              "no-checksum"    = "true"
              remote           = "legacy-use1"
              remotePath       = ""
              tpslimit         = "25"
              "tpslimit-burst" = "10"
              uid              = "1000"
              umask            = "0022"
              "vfs-cache-mode" = "off"
            }
            node_publish_secret_ref { name = local.workspace_s3_secret_name }
          }
        }
        dynamic "volume" {
          for_each = local.workspace_s3_volumes
          content {
            name = volume.key
            csi {
              driver    = "rclone.csi.veloxpack.io"
              read_only = volume.value.read_only
              volume_attributes = {
                gid              = "1000"
                "no-checksum"    = "true"
                remote           = volume.value.remote
                remotePath       = ""
                tpslimit         = "25"
                "tpslimit-burst" = "10"
                uid              = "1000"
                umask            = "0022"
                "vfs-cache-mode" = "off"
              }
              node_publish_secret_ref { name = local.workspace_s3_secret_name }
            }
          }
        }
        # LINT.ThenChange(//src/infra/argocd/components/kyverno/kustomize/workspace-s3-grant-policy.yaml:workspace-s3-credential-contract)
        volume {
          name = "backup-proxy-tmp"
          empty_dir {
            size_limit = "512Mi"
          }
        }
        volume {
          name = "fs"
          empty_dir {
            size_limit = "1Gi"
          }
        }
        volume {
          name = "tmp"
          empty_dir {
            size_limit = local.workspace_tmp_size
          }
        }
        volume {
          name = "tailnet-state"
          empty_dir {
            size_limit = "1Mi"
          }
        }
        volume {
          name = "tailnet-tmp"
          empty_dir {
            size_limit = "64Mi"
          }
        }
        volume {
          name = "workspace"
          persistent_volume_claim {
            claim_name = local.home_volume_claim_name
            read_only  = false
          }
        }
        volume {
          name = "workspace-ca"
          config_map {
            name = kubernetes_config_map_v1.workspace_ca[0].metadata[0].name
            items {
              key  = "ca.crt"
              path = "ca.crt"
            }
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_config_map_v1.access,
    kubernetes_config_map_v1.mise,
    kubernetes_config_map_v1.workspace_ca,
    kubernetes_config_map_v1.workspace_kubeconfig,
    kubernetes_manifest.workspace_s3_credentials,
    kubernetes_secret_v1.snapshot_repository,
    kubernetes_secret_v1.workspace_runtime,
    module.coder_snapshots,
  ]

  lifecycle {
    replace_triggered_by = [terraform_data.workspace_scheduling_contract]

    precondition {
      condition = (
        local.template_preview ||
        (
          can(regex("^[a-z0-9]+(?:-[a-z0-9]+)*$", lower(data.coder_workspace.me.name))) &&
          can(regex("^[a-z0-9]+(?:-[a-z0-9]+)*$", local.coder_owner_name)) &&
          local.coder_app_label_length <= 63
        )
      )
      error_message = "Workspace and owner names must produce a valid Coder application DNS label of at most 63 characters."
    }

    precondition {
      condition = (
        length(regexall("(?m)^BINARY_URL=.*$", coder_agent.main.init_script)) == 1 &&
        length(regexall("(?m)^export CODER_AGENT_URL=.*$", coder_agent.main.init_script)) == 1
      )
      error_message = "Coder's generated agent bootstrap no longer has the expected URL assignments."
    }

    precondition {
      condition     = local.current_owner_attestation.attested == "true" || local.template_preview
      error_message = "A running workspace requires an authenticated immutable OIDC owner binding."
    }

    precondition {
      condition     = local.writer_inventory_valid
      error_message = "The Kubernetes API returned an invalid workspace writer inventory."
    }

    precondition {
      condition     = local.template_preview || length(local.workspace_writer_conflicts) == 0
      error_message = "Another active workspace already writes this owner snapshot lineage; stop it before starting this workspace."
    }

    precondition {
      condition     = alltrue([for segment in local.checkout_segments : !contains([".", ".."], segment)])
      error_message = "Checkout path must not contain dot or parent-directory segments."
    }

    precondition {
      condition     = !contains(local.reserved_checkout_roots, local.checkout_segments[0])
      error_message = "Checkout path must not shadow an operating-system or workspace runtime directory."
    }

    precondition {
      condition     = local.template_preview || can(regex("^[a-z][a-z0-9-]{0,31}$", local.owner_username))
      error_message = "The attested OIDC preferred_username must be a lowercase POSIX-safe and Coder-compatible login name of at most 32 characters."
    }

    precondition {
      condition     = local.template_preview || !contains(local.reserved_usernames, local.owner_username)
      error_message = "The attested OIDC preferred_username matches a reserved system domain label."
    }
  }
}

resource "kubernetes_service_v1" "workspace_ssh" {
  count = local.workspace_start_count

  metadata {
    name      = local.workspace_machine
    namespace = local.workspace_namespace
    labels = merge(local.app_labels, {
      "app.kubernetes.io/component" = "external-dns-source"
    })
    annotations = {
      "external-dns.kubernetes.io/hostname" = "${local.ssh_wildcard_hostname},${local.ssh_alias_hostname}"
    }
  }

  spec {
    type     = "ClusterIP"
    selector = local.app_labels

    port {
      name        = "ssh"
      port        = local.ssh_port
      target_port = 2222
      protocol    = "TCP"
    }
  }
}

resource "terraform_data" "workspace_admission_probe" {
  count = local.workspace_start_count == 1 && !local.template_preview ? 1 : 0

  triggers_replace = [data.external.workspace_build_context.result.build_id]

  depends_on = [kubernetes_deployment_v1.workspace]

  provisioner "local-exec" {
    command = "${path.module}/hooks/wait-for-admission.sh"
    environment = {
      CODER_WORKSPACE_BUILD_ID = data.external.workspace_build_context.result.build_id
      CODER_WORKSPACE_ID       = data.coder_workspace.me.id
      CODER_WORKSPACE_NAME     = data.coder_workspace.me.name
      KUBERNETES_CONFIG_PATH   = var.kubernetes_config_path
      TIMEOUT_SECONDS          = "2400"
      WORKSPACE_CELL           = local.selected_cell
      WORKSPACE_NAMESPACE      = local.workspace_namespace
    }
  }
}
