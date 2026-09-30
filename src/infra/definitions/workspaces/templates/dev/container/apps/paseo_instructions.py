#!/usr/bin/env python3
"""Serves interactive SSH instructions and client setup guides via the authenticated Coder workspace application proxy."""

from __future__ import annotations

import html
import os
import pathlib
import socket
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

HOST = "127.0.0.1"
PORT = 19848

_CSS = """
    :root {
      color-scheme: dark;
      --bg-page: #09090b;
      --bg-card: #18181b;
      --bg-input: #09090b;
      --bg-btn: #27272a;
      --bg-btn-hover: #3f3f46;
      --border-default: #27272a;
      --border-subtle: #3f3f46;
      --text-primary: #ffffff;
      --text-secondary: #a1a1aa;
      --accent-link: #60a5fa;
      --accent-primary-bg: #f4f4f5;
      --accent-primary-text: #09090b;
      --accent-primary-hover: #e4e4e7;
      --success: #22c55e;
      --success-bg: rgba(34, 197, 94, 0.15);
    }
    @media (prefers-color-scheme: light) {
      :root {
        color-scheme: light;
        --bg-page: #f4f4f5;
        --bg-card: #ffffff;
        --bg-input: #f4f4f5;
        --bg-btn: #f4f4f5;
        --bg-btn-hover: #e4e4e7;
        --border-default: #e4e4e7;
        --border-subtle: #d4d4d8;
        --text-primary: #09090b;
        --text-secondary: #71717a;
        --accent-link: #2563eb;
        --accent-primary-bg: #18181b;
        --accent-primary-text: #ffffff;
        --accent-primary-hover: #27272a;
        --success: #16a34a;
        --success-bg: rgba(22, 163, 74, 0.12);
      }
    }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg-page);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 1.5rem;
      box-sizing: border-box;
      -webkit-font-smoothing: antialiased;
    }
    main {
      box-sizing: border-box;
      width: min(46rem, 100%);
      padding: 1.75rem 2rem;
      background: var(--bg-card);
      border: 1px solid var(--border-default);
      border-radius: 0.75rem;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4), 0 8px 10px -6px rgba(0, 0, 0, 0.4);
    }
    .header {
      margin-bottom: 1.25rem;
    }
    h1 {
      margin: 0 0 0.35rem;
      font-size: 1.35rem;
      font-weight: 600;
      letter-spacing: -0.015em;
      color: var(--text-primary);
    }
    .subtitle {
      margin: 0;
      color: var(--text-secondary);
      font-size: 0.875rem;
      line-height: 1.4;
    }
    .subtitle strong {
      color: var(--text-primary);
      font-weight: 600;
    }
    .tabs {
      display: flex;
      gap: 0.5rem;
      border-bottom: 1px solid var(--border-default);
      margin-bottom: 1.5rem;
      overflow-x: auto;
    }
    .tab-btn {
      background: transparent;
      border: none;
      border-bottom: 2px solid transparent;
      padding: 0.5rem 0.75rem;
      font: inherit;
      font-size: 0.875rem;
      font-weight: 500;
      color: var(--text-secondary);
      cursor: pointer;
      margin-bottom: -1px;
      white-space: nowrap;
      transition: color 0.15s ease, border-color 0.15s ease;
    }
    .tab-btn:hover {
      color: var(--text-primary);
    }
    .tab-btn[aria-selected="true"] {
      font-weight: 600;
      border-bottom-color: var(--text-primary);
      color: var(--text-primary);
    }
    .tab-panel {
      display: block;
    }
    .tab-panel[hidden] {
      display: none;
    }
    .section-item {
      margin-bottom: 1.25rem;
    }
    .section-item:last-child {
      margin-bottom: 0;
    }
    .label-title {
      display: block;
      font-weight: 600;
      font-size: 0.875rem;
      color: var(--text-primary);
      margin-bottom: 0.25rem;
    }
    .section-desc {
      font-size: 0.8125rem;
      color: var(--text-secondary);
      margin: 0 0 0.6rem;
      line-height: 1.4;
    }
    .section-desc code, .section-desc strong {
      color: var(--text-primary);
    }
    .command-box {
      display: flex;
      gap: 0.5rem;
      align-items: stretch;
    }
    input, textarea {
      min-width: 0;
      flex: 1;
      padding: 0.55rem 0.75rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.8125rem;
      border: 1px solid var(--border-default);
      border-radius: 0.375rem;
      background: var(--bg-input);
      color: var(--text-primary);
      box-sizing: border-box;
      outline: none;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    input:focus, textarea:focus {
      border-color: var(--accent-link);
      box-shadow: 0 0 0 1px var(--accent-link);
    }
    textarea {
      resize: vertical;
      line-height: 1.5;
    }
    .copy-btn, .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0.5rem 0.875rem;
      font: inherit;
      font-size: 0.8125rem;
      font-weight: 500;
      border-radius: 0.375rem;
      cursor: pointer;
      text-decoration: none;
      white-space: nowrap;
      transition: all 0.15s ease;
      box-sizing: border-box;
    }
    .copy-btn {
      background: var(--bg-btn);
      border: 1px solid var(--border-subtle);
      color: var(--text-primary);
    }
    .copy-btn:hover {
      background: var(--bg-btn-hover);
      border-color: #52525b;
      color: #ffffff;
    }
    .copy-btn.copied {
      background: var(--success-bg);
      border-color: var(--success);
      color: var(--success);
    }
    .actions {
      margin-top: 0.75rem;
    }
    .primary-btn {
      background: var(--accent-primary-bg);
      border: 1px solid var(--accent-primary-bg);
      color: var(--accent-primary-text) !important;
      font-weight: 600;
      box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
    }
    .primary-btn:hover {
      background: var(--accent-primary-hover) !important;
      border-color: var(--accent-primary-hover) !important;
      color: var(--accent-primary-text) !important;
    }
    .primary-btn:active {
      transform: translateY(1px);
    }
"""

_JS = """
    const tabs = document.querySelectorAll('[role="tab"]');
    const panels = document.querySelectorAll('[role="tabpanel"]');

    function switchTab(newTab) {
      tabs.forEach(tab => {
        const isSelected = tab === newTab;
        tab.setAttribute('aria-selected', isSelected ? 'true' : 'false');
        tab.tabIndex = isSelected ? 0 : -1;
      });
      panels.forEach(panel => {
        panel.hidden = panel.id !== newTab.getAttribute('aria-controls');
      });
    }

    tabs.forEach(tab => {
      tab.addEventListener('click', () => switchTab(tab));
      tab.addEventListener('keydown', e => {
        let target = null;
        if (e.key === 'ArrowRight') {
          target = tab.nextElementSibling || tabs[0];
        } else if (e.key === 'ArrowLeft') {
          target = tab.previousElementSibling || tabs[tabs.length - 1];
        }
        if (target) {
          target.focus();
          switchTab(target);
        }
      });
    });

    document.querySelectorAll('.copy-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const target = document.querySelector(btn.dataset.target);
        const text = target.value || target.textContent;
        try {
          await navigator.clipboard.writeText(text);
          const original = btn.textContent;
          btn.textContent = 'Copied!';
          btn.classList.add('copied');
          setTimeout(() => {
            btn.textContent = original;
            btn.classList.remove('copied');
          }, 2000);
        } catch (_) {
          target.focus();
          if (target.select) target.select();
        }
      });
    });
"""


def wait_for_ssh_ready(path: pathlib.Path, timeout_seconds: int = 3600) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("SSH did not become tailnet-visible before the deadline")
        time.sleep(1)


def ssh_is_ready(path: pathlib.Path) -> bool:
    if not path.is_file():
        return False
    try:
        with socket.create_connection((HOST, 2222), timeout=1):
            return True
    except OSError:
        return False


def _render_terminal_panel(ssh_cmd: str, ssh_uri: str) -> str:
    return f"""    <section class="tab-panel" id="panel-terminal" role="tabpanel" aria-labelledby="tab-terminal">
      <div class="section-item">
        <label class="label-title" for="ssh-cmd">SSH Command</label>
        <p class="section-desc">Connect directly in your local terminal.</p>
        <div class="command-box">
          <input id="ssh-cmd" aria-label="SSH command" readonly value="{ssh_cmd}">
          <button class="copy-btn" type="button" data-target="#ssh-cmd">Copy</button>
        </div>
      </div>
      <div class="section-item">
        <label class="label-title" for="ssh-uri">SSH Address</label>
        <p class="section-desc">Open with your default terminal application or SSH handler.</p>
        <div class="command-box">
          <input id="ssh-uri" aria-label="SSH address" readonly value="{ssh_uri}">
          <button class="copy-btn" type="button" data-target="#ssh-uri">Copy</button>
        </div>
        <div class="actions">
          <a class="button primary-btn" href="{ssh_uri}">Open in Terminal</a>
        </div>
      </div>
    </section>"""


def _render_herdr_panel(remote_cmd: str, machine_cmd: str) -> str:
    return f"""    <section class="tab-panel" id="panel-herdr" role="tabpanel" aria-labelledby="tab-herdr" hidden>
      <div class="section-item">
        <label class="label-title" for="herdr-remote-cmd">Quick Remote Attach</label>
        <p class="section-desc">Attach your local Herdr client to the workspace persistent multi-agent runtime.</p>
        <div class="command-box">
          <input id="herdr-remote-cmd" aria-label="Herdr remote command" readonly value="{remote_cmd}">
          <button class="copy-btn" type="button" data-target="#herdr-remote-cmd">Copy</button>
        </div>
      </div>
      <div class="section-item">
        <label class="label-title" for="herdr-machine-cmd">Save Machine to Sidebar</label>
        <p class="section-desc">Add this workspace permanently to your Herdr window alongside local work.</p>
        <div class="command-box">
          <input id="herdr-machine-cmd" aria-label="Herdr machine command" readonly value="{machine_cmd}">
          <button class="copy-btn" type="button" data-target="#herdr-machine-cmd">Copy</button>
        </div>
      </div>
    </section>"""


def _render_vscode_panel(vscode_uri: str) -> str:
    return f"""    <section class="tab-panel" id="panel-vscode" role="tabpanel" aria-labelledby="tab-vscode" hidden>
      <div class="section-item">
        <label class="label-title">VS Code Remote - SSH</label>
        <p class="section-desc">Open this workspace directly in desktop Visual Studio Code.</p>
        <div class="actions">
          <a class="button primary-btn" href="{vscode_uri}">Open in VS Code</a>
        </div>
      </div>
      <div class="section-item" style="margin-top: 1rem;">
        <label class="label-title" for="vscode-uri">Direct URI</label>
        <div class="command-box">
          <input id="vscode-uri" aria-label="VS Code URI" readonly value="{vscode_uri}">
          <button class="copy-btn" type="button" data-target="#vscode-uri">Copy</button>
        </div>
        <p class="section-desc">Requires the <em>Remote - SSH</em> extension installed locally in VS Code.</p>
      </div>
    </section>"""


def _render_paseo_panel(ssh_uri: str) -> str:
    return f"""    <section class="tab-panel" id="panel-paseo" role="tabpanel" aria-labelledby="tab-paseo" hidden>
      <div class="section-item">
        <label class="label-title">Paseo Desktop</label>
        <p class="section-desc">In Paseo, open <strong>Settings &gt; Add host &gt; Remote SSH</strong> and use the SSH address below:</p>
        <div class="command-box">
          <input id="paseo-ssh-uri" aria-label="Paseo SSH address" readonly value="{ssh_uri}">
          <button class="copy-btn" type="button" data-target="#paseo-ssh-uri">Copy</button>
        </div>
      </div>
    </section>"""


def _render_claude_panel(ssh_host: str, ssh_port: str, host_name: str) -> str:
    return f"""    <section class="tab-panel" id="panel-claude" role="tabpanel" aria-labelledby="tab-claude" hidden>
      <div class="section-item">
        <label class="label-title">Claude Code Desktop SSH Environment</label>
        <p class="section-desc">In Claude Desktop, select <strong>Environment &gt; SSH</strong> and configure the connection details:</p>
        <div class="command-box">
          <input id="claude-ssh-host" aria-label="Claude SSH host" readonly value="{ssh_host}">
          <button class="copy-btn" type="button" data-target="#claude-ssh-host">Copy</button>
        </div>
        <p class="section-desc" style="margin-top: 0.5rem;">Or connect with the SSH config alias <code>{host_name}</code> if configured in your <code>~/.ssh/config</code> (port <code>{ssh_port}</code>).</p>
      </div>
    </section>"""


def _render_codex_panel(
    ssh_host: str,
    ssh_port: str,
    host_name: str,
    checkout_path: str = "/fs/depot",
) -> str:
    return f"""    <section class="tab-panel" id="panel-codex" role="tabpanel" aria-labelledby="tab-codex" hidden>
      <div class="section-item">
        <label class="label-title">Codex Desktop Remote Environment</label>
        <p class="section-desc">In Codex Desktop, open <strong>Settings &gt; Environments &gt; SSH</strong> (or <strong>Connections</strong>) to attach this DevPod as a remote workspace:</p>
        <div class="command-box">
          <input id="codex-ssh-host" aria-label="Codex SSH host" readonly value="{ssh_host}">
          <button class="copy-btn" type="button" data-target="#codex-ssh-host">Copy</button>
        </div>
      </div>
      <div class="section-item">
        <label class="label-title" for="codex-remote-path">Remote Project Path</label>
        <p class="section-desc">Default repository checkout directory inside the workspace:</p>
        <div class="command-box">
          <input id="codex-remote-path" aria-label="Codex remote project path" readonly value="{checkout_path}">
          <button class="copy-btn" type="button" data-target="#codex-remote-path">Copy</button>
        </div>
        <p class="section-desc" style="margin-top: 0.5rem;">Or use the SSH config alias <code>{host_name}</code> if configured in your <code>~/.ssh/config</code> (port <code>{ssh_port}</code>).</p>
      </div>
    </section>"""


def _render_config_panel(name: str, config_snippet: str) -> str:
    return f"""    <section class="tab-panel" id="panel-config" role="tabpanel" aria-labelledby="tab-config" hidden>
      <div class="section-item">
        <label class="label-title" for="ssh-config-snippet">~/.ssh/config Configuration</label>
        <p class="section-desc">Add this snippet to your <code>~/.ssh/config</code> to connect simply using <code>ssh {name}</code>:</p>
        <div class="command-box">
          <textarea id="ssh-config-snippet" aria-label="SSH config snippet" readonly rows="5">{config_snippet}</textarea>
          <button class="copy-btn" type="button" data-target="#ssh-config-snippet">Copy</button>
        </div>
      </div>
    </section>"""


def ssh_instruction_page(
    ssh_uri: str,
    vscode_uri: str,
    workspace_name: str | None = None,
) -> bytes:
    parsed = urllib.parse.urlsplit(ssh_uri)
    user = parsed.username or "coder"
    host = parsed.hostname or "localhost"
    port = str(parsed.port or 2222)
    name = (
        workspace_name
        or os.environ.get("WORKSPACE_NAME")
        or (host.split(".")[0] if host else "workspace")
    )

    ssh_cmd = f"ssh -p {port} {user}@{host}"
    panels = "\n".join([
        _render_terminal_panel(
            html.escape(ssh_cmd, quote=True),
            html.escape(ssh_uri, quote=True),
        ),
        _render_herdr_panel(
            html.escape(f"herdr --remote {ssh_uri} --session workspace", quote=True),
            html.escape(
                f'herdr machine add {ssh_uri} --label "{name}" --remote-session workspace',
                quote=True,
            ),
        ),
        _render_vscode_panel(html.escape(vscode_uri, quote=True)),
        _render_paseo_panel(html.escape(ssh_uri, quote=True)),
        _render_claude_panel(
            html.escape(f"{user}@{host}", quote=True),
            html.escape(port, quote=True),
            html.escape(name, quote=True),
        ),
        _render_codex_panel(
            html.escape(f"{user}@{host}", quote=True),
            html.escape(port, quote=True),
            html.escape(name, quote=True),
            checkout_path=html.escape(
                os.environ.get("WORKSPACE_CHECKOUT_PATH")
                or os.environ.get("CHECKOUT_PATH")
                or "/fs/depot",
                quote=True,
            ),
        ),
        _render_config_panel(
            html.escape(name, quote=True),
            html.escape(
                f"Host {name}\n  HostName {host}\n  User {user}\n  Port {port}", quote=True
            ),
        ),
    ])
    escaped_name = html.escape(name, quote=True)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect over SSH — {escaped_name}</title>
  <style>{_CSS}</style>
</head>
<body>
  <main>
    <header class="header">
      <h1>Connect over SSH</h1>
      <p class="subtitle">Access <strong>{escaped_name}</strong> from any device on your workspace tailnet.</p>
    </header>

    <div class="tabs" role="tablist" aria-label="Connection options">
      <button class="tab-btn" role="tab" id="tab-terminal" aria-selected="true" aria-controls="panel-terminal">SSH (Terminal)</button>
      <button class="tab-btn" role="tab" id="tab-herdr" aria-selected="false" aria-controls="panel-herdr" tabindex="-1">Herdr</button>
      <button class="tab-btn" role="tab" id="tab-vscode" aria-selected="false" aria-controls="panel-vscode" tabindex="-1">VS Code</button>
      <button class="tab-btn" role="tab" id="tab-paseo" aria-selected="false" aria-controls="panel-paseo" tabindex="-1">Paseo</button>
      <button class="tab-btn" role="tab" id="tab-claude" aria-selected="false" aria-controls="panel-claude" tabindex="-1">Claude</button>
      <button class="tab-btn" role="tab" id="tab-codex" aria-selected="false" aria-controls="panel-codex" tabindex="-1">Codex</button>
      <button class="tab-btn" role="tab" id="tab-config" aria-selected="false" aria-controls="panel-config" tabindex="-1">~/.ssh/config</button>
    </div>

{panels}
  </main>
  <script>{_JS}</script>
</body>
</html>
""".encode()


class InstructionHandler(BaseHTTPRequestHandler):
    page = b""
    ssh_ready_file = pathlib.Path("/nonexistent")

    def do_GET(self) -> None:
        if self.path == "/healthz":
            ready = ssh_is_ready(self.ssh_ready_file)
            body = b"ok\n" if ready else b"not ready\n"
            content_type = "text/plain; charset=utf-8"
        elif self.path in {"/", "/ssh", "/ssh/"}:
            body = self.page
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(
            HTTPStatus.OK if self.path != "/healthz" or ready else HTTPStatus.SERVICE_UNAVAILABLE
        )
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        del format, args


if __name__ == "__main__":
    ssh_ready_file = pathlib.Path("/var/run/workspace/tailnet/workspace-ssh-ready")
    wait_for_ssh_ready(ssh_ready_file)
    InstructionHandler.ssh_ready_file = ssh_ready_file
    InstructionHandler.page = ssh_instruction_page(
        os.environ["SSH_URI"],
        os.environ["VSCODE_SSH_URI"],
        os.environ.get("WORKSPACE_NAME"),
    )
    ThreadingHTTPServer((HOST, PORT), InstructionHandler).serve_forever()
