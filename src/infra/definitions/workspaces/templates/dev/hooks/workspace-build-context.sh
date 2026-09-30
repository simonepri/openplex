#!/bin/sh
# Emits non-secret Coder build and workspace context metadata as JSON for OpenTofu external data sources.

set -eu

build_id=${CODER_WORKSPACE_BUILD_ID:-}
if [ -n "${build_id}" ] && ! printf '%s\n' "${build_id}" \
  | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'; then
  printf '%s\n' 'Coder workspace build ID is invalid' >&2
  exit 1
fi

timestamp=$(date +%s)
printf '{"build_id":"%s","timestamp":"%s"}\n' "${build_id}" "${timestamp}"
