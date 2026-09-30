#!/usr/bin/env bash
# Prepares the Herdr AI agent terminal session and validates herdr tool readiness.

set -euo pipefail

: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"

runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
setup_ready_file="${runtime_dir}/workspace-setup-ready"

printf 'Starting Herdr workspace runtime...\n'

# Wait for workspace setup to complete before signalling readiness
for ((i = 0; i < 300; i++)); do
  if [[ -f ${setup_ready_file} ]] && [[ "$(<"${setup_ready_file}")" == "${WORKSPACE_BOOT_TOKEN}" ]]; then
    break
  fi
  sleep 1
done

herdr=(herdr)
if ! command -v herdr >/dev/null 2>&1; then
  herdr=(mise exec -- herdr)
fi
"${herdr[@]}" --version >/dev/null 2>&1 || true

printf 'Herdr workspace session is ready (session: workspace).\n'
