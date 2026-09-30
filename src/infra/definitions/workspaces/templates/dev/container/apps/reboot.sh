#!/usr/bin/env bash
# Initiates an authenticated workspace restart transition using the in-pod Coder CLI.

set -euo pipefail

workspace_name="${CODER_WORKSPACE_NAME:-}"
if [[ -z ${workspace_name} ]]; then
  echo "reboot: CODER_WORKSPACE_NAME is not set" >&2
  exit 1
fi

if [[ -z ${CODER_CLIENT_TLS_CA_FILE:-} && -r /tmp/workspace-ca-bundle.crt ]]; then
  export CODER_CLIENT_TLS_CA_FILE=/tmp/workspace-ca-bundle.crt
fi

coder_bin=""
if command -v coder >/dev/null 2>&1; then
  coder_bin="$(command -v coder)"
else
  for candidate in /tmp/coder.*/coder; do
    if [[ -f ${candidate} && -x ${candidate} ]]; then
      coder_bin="${candidate}"
      break
    fi
  done
fi

if [[ -z ${coder_bin} ]]; then
  echo "reboot: coder binary not found in workspace" >&2
  exit 1
fi

echo "Restarting workspace ${workspace_name}..."
exec "${coder_bin}" restart "${workspace_name}" -y "$@"
