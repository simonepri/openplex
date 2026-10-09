# Configures integrated Coder workspace applications including VS Code Web, Paseo, Herdr, and direct SSH endpoints.

locals {
  ssh_wildcard_hostname = "ssh--${lower(data.coder_workspace.me.name)}--${local.owner_username}.${var.coder_app_domain}"
  ssh_alias_hostname    = "${lower(data.coder_workspace.me.name)}.${local.owner_username}.${var.access_alias_domain}"
  ssh_hostname          = local.ssh_alias_hostname
  # Inlined VS Code remote SSH authority & deep link (previously modules/vscode_remote_ssh)
  vscode_remote_ssh_authority = "${local.owner_username}@${local.ssh_hostname}"
  vscode_remote_ssh_url       = "vscode://vscode-remote/ssh-remote+${local.vscode_remote_ssh_authority}${var.checkout_path}"
}

resource "coder_app" "paseo" {
  agent_id     = coder_agent.main.id
  display_name = "Paseo"
  icon         = "https://paseo.sh/logo.svg"
  open_in      = "slim-window"
  order        = 10
  share        = "owner"
  slug         = "paseo"
  subdomain    = true
  tooltip      = "AI agent desktop workspace and local orchestrator"
  url          = "http://127.0.0.1:6768"

  healthcheck {
    url       = "http://127.0.0.1:6768/"
    interval  = 5
    threshold = 12
  }
}

resource "coder_script" "workspace_paseo" {
  agent_id           = coder_agent.main.id
  display_name       = "Paseo"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = file("${path.module}/container/apps/workspace-paseo.sh")
}

resource "coder_script" "code_server" {
  agent_id           = coder_agent.main.id
  display_name       = "VS Code"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = file("${path.module}/container/apps/code-server.sh")
}

resource "coder_script" "workspace_nohang" {
  agent_id           = coder_agent.main.id
  display_name       = "Nohang"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = file("${path.module}/container/apps/workspace-nohang.sh")
}

resource "coder_app" "vscode" {
  agent_id     = coder_agent.main.id
  display_name = "VS Code"
  icon         = "/icon/code.svg"
  order        = 20
  share        = "owner"
  slug         = "vscode"
  subdomain    = true
  tooltip      = "Browser-based Visual Studio Code development environment"
  url          = "http://127.0.0.1:13337/?folder=${urlencode(var.checkout_path)}"

  healthcheck {
    url       = "http://127.0.0.1:13337/healthz"
    interval  = 5
    threshold = 12
  }
}

resource "coder_script" "workspace_zasper" {
  agent_id           = coder_agent.main.id
  display_name       = "Zasper"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = file("${path.module}/container/apps/workspace-zasper.sh")
}

resource "coder_script" "workspace_herdr" {
  agent_id           = coder_agent.main.id
  display_name       = "Herdr"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = file("${path.module}/container/apps/workspace-herdr.sh")
}

resource "coder_script" "workspace_zellij" {
  agent_id           = coder_agent.main.id
  display_name       = "Zellij"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = file("${path.module}/container/apps/workspace-zellij.sh")
}

resource "coder_app" "zasper" {
  agent_id     = coder_agent.main.id
  display_name = "Zasper"
  icon         = "https://zasper.io/static/images/favicon.svg"
  order        = 25
  share        = "owner"
  slug         = "zasper"
  subdomain    = true
  tooltip      = "Interactive notebook and data analysis environment backed by Zasper"
  url          = "http://127.0.0.1:8048/?token=${local.zasper_access_token}"

  healthcheck {
    url       = "http://127.0.0.1:8048/api/health"
    interval  = 5
    threshold = 12
  }
}

resource "coder_app" "persistent_terminal" {
  agent_id     = coder_agent.main.id
  command      = "printf '\\033]11;#282a36\\007\\033]10;#eff0eb\\007' && cd -- /fs && exec zellij attach --create workspace options --theme snazzy --simplified-ui true --show-startup-tips false --show-release-notes false --session-serialization true --serialize-pane-viewport true --scrollback-lines-to-serialize 10000"
  display_name = "Terminal"
  icon         = "/icon/terminal.svg"
  order        = 30
  share        = "owner"
  slug         = "terminal"
  tooltip      = "Persistent terminal session backed by Zellij"
}

resource "coder_app" "herdr" {
  agent_id     = coder_agent.main.id
  command      = "printf '\\033]11;#282a36\\007\\033]10;#eff0eb\\007' && cd -- /fs && exec herdr --session workspace"
  display_name = "Herdr"
  icon         = "https://herdr.dev/assets/logo.svg"
  order        = 15
  share        = "owner"
  slug         = "herdr"
  tooltip      = "AI agent terminal workspace runtime and attention queue"
}

# The slug must not be "ssh": Coder serves subdomain apps at <slug>--<workspace>--<owner>
# under the app domain, which is where the workspace SSH Service publishes its DNS name.
resource "coder_app" "ssh_access" {
  count        = local.ssh_enabled ? 1 : 0
  agent_id     = coder_agent.main.id
  display_name = "SSH"
  icon         = "/icon/terminal.svg"
  open_in      = "slim-window"
  order        = 40
  share        = "owner"
  slug         = "ssh-access"
  subdomain    = true
  tooltip      = "SSH connection instructions and local IDE integration"
  url          = "http://localhost:19848/ssh"

  healthcheck {
    url       = "http://localhost:19848/healthz"
    interval  = 5
    threshold = 12
  }
}

resource "coder_script" "ssh_instructions" {
  count              = local.ssh_enabled ? 1 : 0
  agent_id           = coder_agent.main.id
  display_name       = "SSH"
  run_on_start       = true
  start_blocks_login = false
  script             = "nohup python3 /etc/workspace/access/paseo_instructions.py </dev/null >/tmp/paseo-instructions.log 2>&1 &"
}

resource "coder_app" "restore_snapshot" {
  agent_id     = coder_agent.main.id
  display_name = "Restore Snapshot..."
  external     = true
  icon         = "https://coder-snapshots.${var.access_alias_domain}/static/favicon.svg"
  order        = 50
  slug         = "restore-snapshot"
  tooltip      = "Browse and restore verified snapshots for this workspace"
  url          = "https://coder-snapshots.${var.access_alias_domain}/?owner=${local.owner_username}&workspace=${lower(data.coder_workspace.me.name)}&lineage=${local.workspace_lineage}"
}

resource "coder_script" "workspace_filebrowser" {
  agent_id           = coder_agent.main.id
  display_name       = "File Browser"
  run_on_start       = true
  start_blocks_login = false
  timeout            = 3600
  script             = file("${path.module}/container/apps/filebrowser.sh")
}

resource "coder_app" "filebrowser" {
  agent_id     = coder_agent.main.id
  display_name = "File Browser"
  icon         = "/icon/filebrowser.svg"
  order        = 35
  share        = "owner"
  slug         = "filebrowser"
  subdomain    = true
  tooltip      = "Browse, upload, and download files in your workspace"
  url          = "http://127.0.0.1:13339"

  healthcheck {
    url       = "http://127.0.0.1:13339/health"
    interval  = 5
    threshold = 12
  }
}
