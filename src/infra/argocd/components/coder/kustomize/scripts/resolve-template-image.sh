#!/bin/sh
# Resolves fleet-declared workspace container image tags to immutable digests.

set -eu

: "${WORKSPACE_IMAGE_SOURCE:?}"
: "${WORKSPACE_IMAGE_REPOSITORY:?}"
: "${WORKSPACE_IMAGE_SOURCE_INSECURE:=false}"

if [ "${WORKSPACE_IMAGE_SOURCE_INSECURE}" = true ]; then
  digest="$(/ko-app/crane digest --insecure "${WORKSPACE_IMAGE_SOURCE}")"
else
  digest="$(/ko-app/crane digest "${WORKSPACE_IMAGE_SOURCE}")"
fi
printf '%s@%s\n' "${WORKSPACE_IMAGE_REPOSITORY}" "${digest}" >/state/workspace-image
