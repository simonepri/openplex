#!/usr/bin/env python3
"""Validates environment variable contracts, shell paths, and security constraints in Coder workspace templates."""

import pathlib
import re
import unittest

CPU_OCCURRENCES = 1
READ_ONLY_ROOT_FILESYSTEM_OCCURRENCES = 4


class WorkspaceEnvironmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        template = pathlib.Path(__file__).parent
        cls.main = "\n".join(p.read_text() for p in sorted(template.glob("*.tf")))
        cls.parameters = template.joinpath("parameters.tf").read_text()
        cls.accelerators = template.joinpath("accelerators.tf").read_text()

    def test_coder_supplies_github_credentials_to_git(self) -> None:
        assert re.search(
            r'data "coder_external_auth" "github" \{\s+id\s+=\s+"github"\s+\}', self.main
        )
        assert "data.coder_external_auth.github.access_token" not in self.main
        assert 'name  = "GITHUB_TOKEN"' not in self.main
        assert ".git-credentials" not in self.main
        assert 'api_key_scope = "no_user_data"' not in self.main
        assert (
            'script             = file("${path.module}/container/init/workspace-start.sh")'
            in self.main
        )

    def test_workspace_consumes_dev_queue_quota(self) -> None:
        assert "non-borrowing queue" not in self.parameters
        assert "queued capacity" not in self.parameters
        for field, value in {
            '"availability-class"': '"ha"',
            '"kueue.x-k8s.io/queue-name"': '"ha"',
            '"latency-class"': '"ls"',
        }.items():
            assert re.search(rf"{re.escape(field)}\s+=\s+{re.escape(value)}", self.main)
        assert '"kueue.x-k8s.io/managed" = "true"' in self.main
        assert '"kueue.x-k8s.io/pod-suspending-parent" = "deployment"' in self.main
        assert 'priority_class_name              = "ha-ls"' in self.main
        assert self.main.count('memory = "${data.coder_parameter.memory_gib.value}Gi"') == 1
        assert self.main.count('memory = "${local.workspace_memory_limit_gib}Gi"') == 1
        assert self.main.count("cpu    = data.coder_parameter.cpu.value") == CPU_OCCURRENCES
        assert self.main.count("cpu    = tostring(local.workspace_cpu_limit)") == 1
        assert "node_selector                    = local.accelerator_node_selector" in self.main
        assert not re.search(r"(?m)^\s+toleration \{", self.main)
        assert "for_each = local.pod_tolerations" in self.main
        for field in ("effect", "key", "operator", "toleration_seconds", "value"):
            assert f"toleration.value.{field}" in self.main

    def test_kueue_migration_replaces_only_the_workspace_deployment(self) -> None:
        marker = self.main.split('resource "terraform_data" "workspace_scheduling_contract" {', 1)[
            1
        ].split('resource "kubernetes_deployment_v1" "workspace" {', 1)[0]
        assert 'input = "kueue-deployment-v1"' in marker
        deployment = self.main.split('resource "kubernetes_deployment_v1" "workspace" {', 1)[1]
        assert "replace_triggered_by = [terraform_data.workspace_scheduling_contract]" in deployment
        module_block = self.main.split('module "coder_snapshots" {', 1)[1].split(
            'resource "kubernetes_deployment_v1" "workspace" {', 1
        )[0]
        assert "workspace_scheduling_contract" not in module_block

    def test_cluster_inventory_controls_resource_parameters(self) -> None:
        cluster = self.parameters.index('data "coder_parameter" "cluster"')
        accelerator = self.parameters.index('data "coder_parameter" "accelerator"')
        accelerator_count = self.parameters.index('data "coder_parameter" "accelerator_count"')
        cpu = self.parameters.index('data "coder_parameter" "cpu"')
        cpu_burst = self.parameters.index('data "coder_parameter" "cpu_burst"')
        memory = self.parameters.index('data "coder_parameter" "memory_gib"')
        memory_burst = self.parameters.index('data "coder_parameter" "memory_burst_gib"')
        storage = self.parameters.index('data "coder_parameter" "home_disk_gib"')
        throughput = self.parameters.index('data "coder_parameter" "disk_throughput_mbps"')
        iops = self.parameters.index('data "coder_parameter" "disk_iops"')
        assert cluster < accelerator
        assert accelerator < accelerator_count
        assert accelerator_count < cpu
        assert cpu < cpu_burst
        assert cpu_burst < memory
        assert memory < memory_burst
        assert memory_burst < storage
        assert storage < throughput
        assert throughput < iops
        assert "sort(keys(local.workspace_placement_inventory))" in self.parameters
        assert 'display_name = "Accelerators"' in self.parameters
        assert 'name  = "None"' in self.parameters
        assert "disabled = length(local.available_accelerator_offers) == 0" in self.parameters
        assert 'data "coder_parameter" "gpu_count"' not in self.parameters
        assert 'data "coder_parameter" "gpu_unavailable"' not in self.parameters
        assert re.search(
            r'data "coder_parameter" "accelerator_count" \{[\s\S]*?'
            r'count\s+= data\.coder_parameter\.accelerator\.value == "__none__" \? 0 : \('
            r"\s*local\.accelerator_count_configurable \? 1 : 0\s*\)"
            r'[\s\S]*?default\s+= "1"'
            r"[\s\S]*?min\s+= 1"
            r"[\s\S]*?max\s+= local\.selected_accelerator_offer\.max_count",
            self.parameters,
        )
        assert (
            "length(data.coder_parameter.accelerator_count) == (local.accelerator_count_configurable ? 1 : 0)"
            in self.main
        )
        for order in range(20, 32):
            assert f"order        = {order}" in self.parameters
        for message in (
            "selected cluster must be present in the published workspace placement inventory",
            "Workspace CPU request must be a whole vCPU value",
            "Workspace CPU burst must be a whole non-negative vCPU value",
            "Workspace memory request must be a whole GiB value",
            "Workspace memory burst must be a whole non-negative GiB value",
            "Workspace storage must be a whole GiB value",
            "selected accelerator must be available in the selected cluster",
            "complete TPU topology",
        ):
            assert message in self.main

    def test_ssh_checkbox_locks_but_retains_the_key_when_disabled(self) -> None:
        assert re.search(
            r'data "coder_parameter" "ssh_enabled" \{[\s\S]*?'
            r'default\s+= "false"[\s\S]*?type\s+= "bool"[\s\S]*?'
            r'form_type\s+= "checkbox"',
            self.parameters,
        )
        assert 'disabled = data.coder_parameter.ssh_enabled.value != "true"' in self.parameters
        assert "Disabling SSH keeps the key for later use." in self.parameters

    def test_persistent_terminal_resurrects_one_named_zellij_session(self) -> None:
        marker = 'resource "coder_app" "persistent_terminal" {'
        app = self.main.split(marker, 1)[1].split("\n}", 1)[0]
        assert 'display_name = "Terminal"' in app
        assert 'slug         = "terminal"' in app
        assert (
            "command      = \"printf '\\\\033]11;#282a36\\\\007\\\\033]10;#eff0eb\\\\007' && "
            "cd -- /fs && exec zellij attach "
            "--create workspace options --theme snazzy --simplified-ui true --show-startup-tips false "
            "--show-release-notes false --session-serialization true "
            '--serialize-pane-viewport true --scrollback-lines-to-serialize 10000"' in app
        )
        assert "url" not in app
        assert "web_terminal           = false" in self.main

    def test_herdr_app_attaches_to_named_agent_session(self) -> None:
        marker = 'resource "coder_app" "herdr" {'
        assert marker in self.main
        app = self.main.split(marker, 1)[1].split("\n}", 1)[0]
        assert 'display_name = "Herdr"' in app
        assert 'slug         = "herdr"' in app
        assert 'icon         = "https://herdr.dev/assets/logo.svg"' in app
        assert "order        = 15" in app
        assert 'share        = "owner"' in app
        assert (
            "command      = \"printf '\\\\033]11;#282a36\\\\007\\\\033]10;#eff0eb\\\\007' && "
            'cd -- /fs && exec herdr --session workspace"' in app
        )
        assert 'tooltip      = "AI agent terminal workspace runtime and attention queue"' in app
        assert "url" not in app

    def test_restore_selector_accepts_catalog_handoff_without_cached_options(
        self,
    ) -> None:
        restore = re.search(
            r'data "coder_parameter" "restore_selector" \{(?P<body>.*?)\n\}',
            self.parameters,
            re.DOTALL,
        )
        if restore is None:
            self.fail("restore_selector parameter is missing")
        body = restore.group("body")
        assert 'form_type    = "input"' in body
        assert 'default      = ""' in body
        assert "option {" not in body
        assert 'dynamic "option"' not in body
        assert "https://coder-snapshots.${var.access_alias_domain}" in body
        assert 'data "coder_parameter" "restore_source_cell"' not in self.parameters
        assert (
            'regex = "^$|^${local.start_fresh_selector}$|${local.snapshot_selector_pattern}"'
            in body
        )
        selector_validation = re.search(
            r'(?m)^\s+snapshot_selector_pattern\s+= "(?P<regex>[^"]+)"$',
            self.main,
        )
        if selector_validation is None:
            self.fail("snapshot selector validation is missing")
        selector_pattern = re.compile(f"^$|^__start-fresh__$|{selector_validation.group('regex')}")
        for accepted in (
            "",
            "__start-fresh__",
            "manifest.with-dots_1",
            "cell-eaws-lh1/manifest.with-dots_1",
        ):
            assert re.search(selector_pattern, accepted)
        for rejected in ("_not-the-legacy-sentinel", "owner/manifest", "a" * 129):
            assert not re.search(selector_pattern, rejected)
        assert re.search(
            r"restore_selector\s+=\s+data\.coder_parameter\.restore_selector\.value", self.main
        )
        assert "snapshot_selector_pattern = " in self.main

    def test_gpu_catalog_carries_scheduling_and_selection_metadata(self) -> None:
        for capacity_type in ("on-demand", "spot"):
            assert f'"{capacity_type}"' in self.main
        for model in (
            "a10",
            "a100-40gb",
            "a100-80gb",
            "a10g",
            "b200",
            "b300",
            "h100",
            "h100-nvl-94gb",
            "h200",
            "l4",
            "l40s",
            "rtx-pro-server-6000",
            "t4",
            "v100",
        ):
            assert f'"{model}"' in self.main
        assert "GB VRAM. Up to %d per workspace." in self.main
        assert 'offer_key == "${offer.model}-${offer.capacity_type}"' in self.main
        assert (
            "accelerator_node_selector = local.accelerator_placement_enabled ?" in self.accelerators
        )
        assert 'local.selected_accelerator_offer.kind == "gpu"' in self.main
        assert "local.selected_offer.node_selector.key" in self.accelerators
        assert "local.selected_offer.resource" in self.accelerators
        assert "node.kubernetes.io/instance-type" not in self.accelerators
        assert "karpenter.k8s.aws/" not in self.accelerators
        assert "cloud.google.com/" not in self.accelerators

    def test_workspace_writable_paths_and_sidecars_are_bounded(self) -> None:
        assert (
            self.main.count("read_only_root_filesystem  = true")
            == READ_ONLY_ROOT_FILESYSTEM_OCCURRENCES
        )
        for name, size_limit in {
            "backup-proxy-tmp": "512Mi",
            "tailnet-state": "1Mi",
            "tailnet-tmp": "64Mi",
        }.items():
            pattern = rf'name = "{name}"\s+empty_dir {{\s+size_limit = "{size_limit}"\s+}}'
            assert re.search(pattern, self.main)
        assert re.search(
            r'name = "tmp"\s+empty_dir \{\s+size_limit = local\.workspace_tmp_size\s+\}',
            self.main,
        )
        assert '"--disable=PutStream",' in self.main
        assert re.search(
            r'module "coder_snapshots" \{[\s\S]*?'
            r"home_disk_gib\s+= data\.coder_parameter\.home_disk_gib\.value[\s\S]*?"
            r"storage_class_name\s+= var\.storage_class_name",
            self.main,
        )
        assert self.main.count('backup_proxy = { cpu = "25m", memory = "64Mi" }') == 1
        assert self.main.count('tailnet      = { cpu = "10m", memory = "32Mi" }') == 1
        for requests, limits in (
            (
                "requests = local.workspace_sidecar_requests.backup_proxy",
                'limits   = { cpu = "500m", memory = "256Mi" }',
            ),
            (
                "requests = local.workspace_sidecar_requests.tailnet",
                'limits   = { cpu = "250m", memory = "128Mi" }',
            ),
        ):
            assert self.main.count(requests) == 1
            assert self.main.count(limits) == 1
        assert self.main.index('name              = "tailnet"') < self.main.index(
            'name = "backup-proxy"'
        ), (
            "the tailnet dependency must outlive the backup proxy during "
            "reverse-order sidecar shutdown"
        )

    def test_workspace_cache_environment_is_wired(self) -> None:
        assert "for_each = local.workspace_cache_environment" in self.main
        for env_var in ("BAZEL_OUTPUT_ROOT", "CARGO_TARGET_DIR", "GOCACHE", "XDG_CACHE_HOME"):
            assert env_var in self.main

    def test_authenticated_owner_is_the_workspace_process_identity(self) -> None:
        assert re.search(
            r'env \{\s+name\s+=\s+"WORKSPACE_USERNAME"\s+'
            r"value\s+=\s+local\.owner_username\s+\}",
            self.main,
        )
        assert 'USER=\\"$WORKSPACE_USERNAME\\" LOGNAME=\\"$WORKSPACE_USERNAME\\"' in self.main

    def test_workload_registry_reaches_workspace_commands(self) -> None:
        for name, value in {
            "WORKLOAD_REGISTRY": "local.workload_registry",
            "WORKLOAD_REGISTRY_INSECURE": "local.workload_registry_insecure",
        }.items():
            assert re.search(rf"(?m)^    {name}\s+= {value}$", self.main)
        assert "env = merge({" not in self.main
        assert self.main.count("for_each = local.workload_origin_environment") == 1
        assert re.search(
            r'dynamic "env" \{\s+for_each = local\.workload_origin_environment\s+'
            r"content \{\s+name\s+= env\.key\s+value\s+= env\.value\s+\}\s+\}",
            self.main,
        )

    def test_shared_workspaces_namespace_owns_template_resources(self) -> None:
        assert 'data "coder_parameter" "project"' not in self.parameters
        assert "WORKSPACE_PROJECT" not in self.main
        assert re.search(r'workspace_namespace\s+=\s+"workspaces"', self.main)
        assert 'variable "workspace_namespace"' not in self.main
        assert 'variable "team"' not in self.main
        assert "WORKSPACE_TEAM" not in self.main

    def test_accelerator_offers_exclude_spot_capacity(self) -> None:
        """Dev workspaces must never expose or use spot capacity."""
        assert 'if offer.capacity_type != "spot"' in self.accelerators
        assert "disabled = length(local.available_accelerator_offers) == 0" in self.parameters

    def test_periodic_workspace_backup_contract(self) -> None:
        assert re.search(r'snapshot_interval\s+=\s+"0 \*/30 \* \* \* \*\"', self.main)
        assert 'module "coder_snapshots"' in self.main
        workspace_snapshots = (
            pathlib.Path(__file__).parent / "container" / "init" / "workspace-snapshots.sh"
        ).read_text()
        assert (
            'policy set "${workspace_volume}" --keep-latest 3 --keep-hourly 12 --keep-daily 7 --keep-weekly 4 --keep-monthly 0 --keep-annual 0'
            in workspace_snapshots
        )

    def test_workspace_mounts_shared_cell_s3_storage(self) -> None:
        assert 'name  = "AWS_PROFILE"' not in self.main
        assert 'name  = "AWS_SHARED_CREDENTIALS_FILE"' in self.main
        assert 'value = "/var/run/workspace/s3/credentials"' in self.main
        assert 'mount_path = "/fs/s3/${local.selected_virtual_name}/home/legacy"' in self.main
        assert 'mount_path = "/fs/s3/aws-use1/home/legacy"' in self.main
        assert 'name       = "legacy"' in self.main
        assert 'name       = "legacy-use1"' in self.main
        node_publish_refs = re.findall(r"node_publish_secret_ref\s*\{[^}]*\}", self.main)
        assert len(node_publish_refs) > 0
        for ref in node_publish_refs:
            assert "name = local.workspace_s3_secret_name" in ref
        assert 'secret_name  = "workspace-s3"' not in self.main
        assert '"workspace-s3"' not in self.main
        assert 'driver = "rclone.csi.veloxpack.io"' in self.main
        assert 'name = "workspace-s3-credentials"' in self.main
        for volume_name in ("legacy", "legacy-use1"):
            assert re.search(
                rf'volume\s*\{{\s*name\s*=\s*"{volume_name}"\s*csi\s*\{{\s*driver\s*=\s*"rclone\.csi\.veloxpack\.io"\s*volume_attributes\s*=\s*\{{\s*gid\s*=\s*"1000"\s*"no-checksum"\s*=\s*"true"\s*remote\s*=\s*"{volume_name}"\s*remotePath\s*=\s*""\s*tpslimit\s*=\s*"25"\s*"tpslimit-burst"\s*=\s*"10"\s*uid\s*=\s*"1000"\s*umask\s*=\s*"0022"\s*"vfs-cache-mode"\s*=\s*"off"\s*\}}',
                self.main,
            )

    def test_workspace_mounts_snapshot_repository_secret_and_password_file(self) -> None:
        assert 'name  = "KOPIA_PASSWORD_FILE"' in self.main
        assert 'value = "/var/run/workspace/snapshot-repository/password"' in self.main
        assert "KOPIA_SNAPSHOT_BROKER_URL" not in self.main
        assert 'mount_path = "/var/run/workspace/snapshot-repository"' in self.main
        assert "secret_name  = local.snapshot_repository_secret_name" in self.main

    def test_workspace_sets_aws_checksum_calculation_and_validation(self) -> None:
        assert 'name  = "AWS_REQUEST_CHECKSUM_CALCULATION"' in self.main
        assert 'name  = "AWS_RESPONSE_CHECKSUM_VALIDATION"' in self.main
        for env_name in ("AWS_REQUEST_CHECKSUM_CALCULATION", "AWS_RESPONSE_CHECKSUM_VALIDATION"):
            assert re.search(
                rf'env\s*\{{\s*name\s*=\s*"{env_name}"\s*value\s*=\s*"when_required"\s*\}}',
                self.main,
            )

    def test_rclone_csi_s3_volumes_no_checksum(self) -> None:
        rclone_volume_count = len(
            re.findall(r'driver\s+=\s+"rclone\.csi\.veloxpack\.io"', self.main)
        )
        no_checksum_count = len(re.findall(r'"no-checksum"\s+=\s+"true"', self.main))
        assert rclone_volume_count > 0
        assert rclone_volume_count == no_checksum_count
        assert '"ignore-checksum"' not in self.main

    def test_workspace_disk_performance_parameters(self) -> None:
        assert re.search(
            r'data "coder_parameter" "disk_throughput_mbps" \{[\s\S]*?'
            r"count\s+= local\.is_ebs_storage \? 1 : 0[\s\S]*?"
            r'display_name\s+= "Disk sequential throughput \(MB/s\)"[\s\S]*?'
            r'default\s+= "500"[\s\S]*?'
            r'form_type\s+= "slider"[\s\S]*?'
            r"mutable\s+= true[\s\S]*?"
            r"order\s+= 27[\s\S]*?"
            r"min\s+= 125[\s\S]*?"
            r"max\s+= 1000",
            self.parameters,
        )
        assert re.search(
            r'data "coder_parameter" "disk_iops" \{[\s\S]*?'
            r"count\s+= local\.is_ebs_storage \? 1 : 0[\s\S]*?"
            r'display_name\s+= "Disk random 4K IOPS"[\s\S]*?'
            r'default\s+= "8000"[\s\S]*?'
            r'form_type\s+= "slider"[\s\S]*?'
            r"mutable\s+= true[\s\S]*?"
            r"order\s+= 28[\s\S]*?"
            r"min\s+= 3000[\s\S]*?"
            r"max\s+= 16000",
            self.parameters,
        )
        assert re.search(
            r'module "coder_snapshots" \{[\s\S]*?'
            r"disk_iops\s+= local\.disk_iops[\s\S]*?"
            r"disk_throughput_mbps\s+= local\.disk_throughput_mbps",
            self.main,
        )
        assert (
            'is_ebs_storage         = var.storage_class_name == "general-expandable"' in self.main
        )

    def test_workspace_rejects_reserved_owner_usernames(self) -> None:
        for reserved in (
            "argocd",
            "atlantis",
            "buildbuddy",
            "coder",
            "dex",
            "grafana",
            "headlamp",
            "hooks",
            "kube",
            "s3",
            "signoz",
        ):
            assert f'"{reserved}"' in self.main
        assert "!contains(local.reserved_usernames, local.owner_username)" in self.main

    def test_ssh_button_does_not_claim_the_ssh_dns_name(self) -> None:
        # A subdomain app slug "ssh" would be served at ssh--<ws>--<owner>, the SSH Service's name.
        assert 'resource "coder_app" "ssh_access"' in self.main
        assert 'slug         = "ssh-access"' in self.main
        assert not re.search(r'slug\s*=\s*"ssh"', self.main)

    def test_workspace_ssh_service_and_routing_contract(self) -> None:
        assert (
            '"external-dns.kubernetes.io/hostname" = "${local.ssh_wildcard_hostname},${local.ssh_alias_hostname}"'
            in self.main
        )
        assert 'variable "coder_app_domain"' in self.main
        assert re.search(
            r'resource\s+"kubernetes_service_v1"\s+"workspace_ssh"\s*\{[\s\S]*?'
            r"port\s*\{[\s\S]*?port\s*=\s*local\.ssh_port[\s\S]*?"
            r"target_port\s*=\s*2222",
            self.main,
        )


if __name__ == "__main__":
    unittest.main()
