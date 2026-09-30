#!/usr/bin/env bash
# Prepares the persistent Zellij terminal session environment.

set -euo pipefail

: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"

runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
setup_ready_file="${runtime_dir}/workspace-setup-ready"

printf 'Starting persistent Zellij terminal session...\n'

# Wait for workspace setup to complete before signalling readiness
for ((i = 0; i < 300; i++)); do
  if [[ -f ${setup_ready_file} ]] && [[ "$(<"${setup_ready_file}")" == "${WORKSPACE_BOOT_TOKEN}" ]]; then
    break
  fi
  sleep 1
done

printf 'Zellij workspace session is ready (session: workspace, theme: snazzy).\n'
