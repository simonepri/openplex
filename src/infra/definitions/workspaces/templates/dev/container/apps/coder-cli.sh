#!/usr/bin/env bash
# Resolves and executes the in-pod Coder CLI binary and translates target aliases.

set -euo pipefail

if [[ -z ${CODER_CLIENT_TLS_CA_FILE:-} && -r /tmp/workspace-ca-bundle.crt ]]; then
  export CODER_CLIENT_TLS_CA_FILE=/tmp/workspace-ca-bundle.crt
fi

coder_bin=""
for candidate in /tmp/coder.*/coder; do
  if [[ -f ${candidate} && -x ${candidate} ]]; then
    coder_bin="${candidate}"
    break
  fi
done

if [[ -z ${coder_bin} ]]; then
  echo "coder: binary not found in workspace" >&2
  exit 1
fi

args=()
for arg in "$@"; do
  if [[ ${arg} == "self" && -n ${CODER_WORKSPACE_NAME:-} ]]; then
    args+=("${CODER_WORKSPACE_NAME}")
  else
    args+=("${arg}")
  fi
done

exec "${coder_bin}" "${args[@]}"
