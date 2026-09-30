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

    def test_workspace_consumes_team_ha_quota(self) -> None:
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
        assert cluster < accelerator
        assert accelerator < accelerator_count
        assert accelerator_count < cpu
        assert cpu < cpu_burst
        assert cpu_burst < memory
        assert memory < memory_burst
        assert memory_burst < storage
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
        for order in range(20, 30):
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
        assert (
            "restore_selector            = data.coder_parameter.restore_selector.value" in self.main
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

    def test_registration_endpoint_reaches_the_provisioner(self) -> None:
        registration = self.main.split(
            'resource "terraform_data" "workspace_agent_registration" {', 1
        )[1].split('resource "coder_script" "workspace_start" {', 1)[0]
        for name, value in {
            "CODER_WORKSPACE_AGENT_REGISTRATION_TOKEN_FILE": (
                "/var/run/cluster/workspace-agent-registration/token"
            ),
            "CODER_WORKSPACE_AGENT_REGISTRATION_URL": (
                "https://headscale-workspace-registration.headscale.svc.cluster.local:8443/"
                "v1/workspace-agents/register"
            ),
        }.items():
            assert re.search(rf'(?m)^\s+{name}\s+= "{re.escape(value)}"$', registration)

    def test_team_workspaces_namespace_owns_template_resources(self) -> None:
        assert 'data "coder_parameter" "project"' not in self.parameters
        assert "WORKSPACE_PROJECT" not in self.main
        assert "workspace_namespace     = var.workspace_namespace" in self.main
        assert 'local.workspace_namespace == "team-${var.team}-workspaces"' in self.main

    def test_periodic_workspace_backup_contract(self) -> None:
        assert 'snapshot_interval           = "0 */30 * * * *"' in self.main
        assert 'module "coder_snapshots"' in self.main
        workspace_snapshots = (
            pathlib.Path(__file__).parent / "container" / "init" / "workspace-snapshots.sh"
        ).read_text()
        assert (
            'policy set "${workspace_volume}" --keep-latest 3 --keep-hourly 12 --keep-daily 7 --keep-weekly 4 --keep-monthly 0 --keep-annual 0'
            in workspace_snapshots
        )


if __name__ == "__main__":
    unittest.main()
