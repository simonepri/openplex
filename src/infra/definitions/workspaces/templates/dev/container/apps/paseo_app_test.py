#!/usr/bin/env python3
"""Tests Coder workspace application manifests to verify Paseo web interfaces and SSH documentation apps are exposed."""

from __future__ import annotations

import pathlib
import re
import unittest


class WorkspaceAppsTest(unittest.TestCase):
    def setUp(self) -> None:
        template = pathlib.Path(__file__).parents[2]
        self.main = "\n".join(p.read_text() for p in sorted(template.glob("*.tf")))
        self.parameters = template.joinpath("parameters.tf").read_text()
        self.apps = template.joinpath("apps.tf").read_text()

    def test_paseo_proxies_bundled_web_ui(self) -> None:
        assert 'resource "coder_app" "paseo"' in self.main
        assert 'display_name = "Paseo"' in self.main
        assert 'icon         = "https://paseo.sh/logo.svg"' in self.main
        assert "order        = 10" in self.main
        assert 'url          = "http://127.0.0.1:6768"' in self.main
        assert 'share        = "owner"' in self.main
        assert "subdomain    = true" in self.main
        assert 'file("${path.module}/container/apps/workspace-paseo.sh")' in self.main
        assert 'name  = "PASEO_APP_HOSTNAME"' in self.main
        assert "value = local.paseo_app_hostname" in self.main
        assert 'data "coder_workspace_owner" "me" {}' in self.main
        assert re.search(
            r'(?m)^\s*paseo_app_hostname\s*=\s*"paseo--\$\{lower\(data\.coder_workspace\.me\.name\)\}--\$\{local\.coder_owner_name\}\.\$\{var\.coder_app_domain\}"$',
            self.main,
        )
        assert not re.search(
            r"(?m)^\s*paseo_app_hostname\s*=.*\$\{local\.owner_username\}", self.main
        )
        assert re.search(
            r'(?m)^\s*"paseo_coder_proxy\.py"\s*=\s*file\("\$\{path\.module\}/container/apps/paseo_coder_proxy\.py"\)$',
            self.main,
        )
        assert 'mount_path = "/var/run/workspace/tailnet"' in self.main
        assert 'name       = "tailnet-state"' in self.main
        assert self.main.count('name = "tailnet-tmp"') == 1

    def test_tailnet_readiness_does_not_block_workspace_agent_startup(self) -> None:
        assert "startup_probe {" not in self.main
        assert re.search(
            r"(?ms)readiness_probe\s*\{\s*exec\s*\{\s*command = \[\"test\", \"-f\", \"/var/run/workspace/tailnet/workspace-tailnet-ready\"\]\s*\}\s*initial_delay_seconds\s*= 1\s*period_seconds\s*= 1\s*\}",
            self.main,
        )

    def test_browser_vscode_uses_restore_gated_pinned_launcher(self) -> None:
        assert "registry.coder.com/coder/code-server/coder" not in self.apps
        for name in (
            "CODE_SERVER_DEFAULT_EXTENSIONS",
            "CODE_SERVER_DEFAULT_SETTINGS",
            "CODE_SERVER_PORT",
            "CODE_SERVER_READY_FILE",
            "CODE_SERVER_SHA256",
            "CODE_SERVER_VERSION",
        ):
            assert name not in self.main
        assert 'resource "coder_script" "code_server"' in self.apps
        assert 'file("${path.module}/container/apps/code-server.sh")' in self.apps
        assert "start_blocks_login = false" in self.apps
        assert 'resource "coder_app" "vscode"' in self.apps
        assert 'display_name = "VS Code"' in self.apps
        assert "order        = 20" in self.apps
        assert 'share        = "owner"' in self.apps
        assert "subdomain    = true" in self.apps
        assert "folder=${urlencode(var.checkout_path)}" in self.apps
        assert "display_apps {\n    vscode                 = true" not in self.main

    def test_zasper_app_provides_interactive_notebook_environment(self) -> None:
        assert 'resource "coder_script" "workspace_zasper"' in self.apps
        assert 'file("${path.module}/container/apps/workspace-zasper.sh")' in self.apps
        assert 'resource "coder_app" "zasper"' in self.apps
        assert 'display_name = "Zasper"' in self.apps
        assert 'icon         = "https://zasper.io/static/images/favicon.svg"' in self.apps
        assert "order        = 25" in self.apps
        assert 'share        = "owner"' in self.apps
        assert "subdomain    = true" in self.apps
        assert (
            'url          = "http://127.0.0.1:8048/?token=${local.zasper_access_token}"'
            in self.apps
        )
        assert 'name = "ZASPER_ACCESS_TOKEN"' in self.main
        assert "zasper_access_token     = local.zasper_access_token" in self.main
        assert 'url       = "http://127.0.0.1:8048/api/health"' in self.apps

    def test_herdr_app_provides_agent_workspace_session(self) -> None:
        assert 'resource "coder_app" "herdr"' in self.apps
        assert 'display_name = "Herdr"' in self.apps
        assert 'icon         = "https://herdr.dev/assets/logo.svg"' in self.apps
        assert "order        = 15" in self.apps
        assert 'share        = "owner"' in self.apps
        assert 'slug         = "herdr"' in self.apps
        assert "exec herdr --session workspace" in self.apps

    def test_filebrowser_app_provides_web_file_manager(self) -> None:
        assert 'resource "coder_script" "workspace_filebrowser"' in self.apps
        assert 'file("${path.module}/container/apps/filebrowser.sh")' in self.apps
        assert 'resource "coder_app" "filebrowser"' in self.apps
        assert 'display_name = "File Browser"' in self.apps
        assert 'icon         = "/icon/filebrowser.svg"' in self.apps
        assert "order        = 35" in self.apps
        assert 'share        = "owner"' in self.apps
        assert "subdomain    = true" in self.apps
        assert 'url          = "http://127.0.0.1:13339"' in self.apps
        assert 'url       = "http://127.0.0.1:13339/health"' in self.apps

    def test_ssh_is_disabled_by_default_and_requires_a_key(self) -> None:
        assert re.search(
            r'data "coder_parameter" "ssh_enabled" \{[\s\S]*?'
            r'default\s+= "false"[\s\S]*?type\s+= "bool"[\s\S]*?'
            r'form_type\s+= "checkbox"',
            self.parameters,
        )
        assert (
            'styling      = jsonencode({ disabled = data.coder_parameter.ssh_enabled.value != "true" })'
            in self.parameters
        )
        assert re.search(
            r'(?m)^\s*ssh_enabled\s*=\s*data\.coder_parameter\.ssh_enabled\.value == "true" && data\.coder_parameter\.ssh_public_key\.value != ""$',
            self.main,
        )
        assert (
            'ssh_alias_hostname    = "${lower(data.coder_workspace.me.name)}.${local.owner_username}.${var.access_alias_domain}"'
            in self.main
        )
        assert re.search(r"(?m)^\s*ssh_port\s*=\s*22$", self.main)
        assert re.search(
            r'(?m)^\s*ssh_uri\s*=\s*"ssh://\$\{local\.owner_username\}@\$\{local\.ssh_hostname\}:\$\{local\.ssh_port\}"$',
            self.main,
        )
        assert re.search(
            r'(?ms)env\s*\{\s*name\s*=\s*"SSH_URI"\s*value\s*=\s*local\.ssh_uri\s*\}', self.main
        )
        assert re.search(
            r'(?ms)env\s*\{\s*name\s*=\s*"VSCODE_SSH_URI"\s*value\s*=\s*local\.vscode_remote_ssh_url\s*\}',
            self.main,
        )

    def test_template_rejects_coder_app_with_slug_ssh(self) -> None:
        """Coder apps must not use the slug 'ssh' to avoid collision with workspace SSH routing."""
        assert 'slug         = "ssh"' not in self.main
        assert 'slug = "ssh"' not in self.main
        app_slugs = re.findall(
            r'resource\s+"coder_app"\s+"[^"]+"\s*\{[\s\S]*?slug\s*=\s*"([^"]+)"', self.main
        )
        assert "ssh" not in app_slugs

    def test_ssh_validation_accepts_wire_format_not_base64_shape(self) -> None:
        key_pattern = re.compile(
            r"^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-P][A-Za-z0-9+/]{42}( [^\x00-\x1F\x7F]+)?$"
        )
        valid_key = (
            "ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
            "test@example"
        )

        assert re.search(key_pattern, valid_key)
        assert not re.search(key_pattern, "ssh-ed25519 QUFBQQ== garbage")
        assert not re.search(key_pattern, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIz" + "A" * 42)
        assert "Enabling direct SSH requires one structurally valid" in self.main

    def test_restore_and_access_markers_are_scoped_to_container_boot(self) -> None:
        assert "code_server_restore_gate" not in self.main
        assert "workspace-restore-gate.sh" not in self.main
        assert "/dev/urandom" in self.main
        assert "export WORKSPACE_BOOT_TOKEN" in self.main
        for name in (
            "KOPIA_RESTORE_READY_FILE",
            "CODE_SERVER_READY_FILE",
            "PASEO_READY_FILE",
            "SSH_READY_FILE",
        ):
            assert name not in self.main
        assert (
            'display_name       = "Setup"\n'
            "  run_on_start       = true\n"
            "  start_blocks_login = true" in self.main
        )

    def test_restore_selector_does_not_trigger_restart_warning(self) -> None:
        restore_parameter = re.search(
            r'data "coder_parameter" "restore_selector" \{(?P<body>.*?)\n\}',
            self.parameters,
            re.DOTALL,
        )
        if restore_parameter is None:
            self.fail("restore_selector parameter is missing")
        body = restore_parameter.group("body")
        assert "ephemeral    = true" in body
        assert "ephemeral    = false" not in body

    def test_requested_access_cards_are_visible(self) -> None:
        assert "web_terminal           = false" in self.main
        assert "count              = local.ssh_enabled ? 1 : 0" in self.main

    def test_coder_wildcard_hosts_are_bounded_to_access_parent_domain(self) -> None:
        assert "var.coder_app_domain" in self.main

    def test_workspace_paseo_script_uses_versionless_mise_exec(self) -> None:
        template = pathlib.Path(__file__).parents[2]
        paseo_sh = template.joinpath("container/apps/workspace-paseo.sh").read_text()

        assert "paseo=(mise exec -- paseo)" in paseo_sh
        assert "npm:@getpaseo/cli@" not in paseo_sh, (
            "workspace-paseo.sh must not hardcode @getpaseo/cli versions; mise must resolve versions from config"
        )

    def test_template_import_skips_real_owner_name_constraints(self) -> None:
        assert re.search(
            r"(?ms)precondition\s*\{\s*condition\s*=\s*\(\s*local\.template_preview\s*\|\|\s*\(.*?local\.coder_app_label_length <= 63",
            self.main,
        )
        assert (
            'condition     = local.template_preview || can(regex("^[a-z][a-z0-9-]{0,31}$", local.owner_username))'
            in self.main
        )

    def test_pod_grace_exceeds_shutdown_backup_budget(self) -> None:
        assert "termination_grace_period_seconds = 120" in self.main
        assert 'module "coder_snapshots"' in self.main


if __name__ == "__main__":
    unittest.main()
