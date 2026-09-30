#!/usr/bin/env bash
# Publishes the workspace image under its declared discovery tag for template reconciliation.

set -euo pipefail

if [[ -z ${RUNFILES_DIR:-} && -d "$0.runfiles" ]]; then
  export RUNFILES_DIR="$0.runfiles"
fi

pusher="${1:?image pusher is required}"
inventory="${2:?workspace image inventory is required}"
jq="${3:?Bazel jq is required}"
: "${WORKLOAD_REGISTRY:?set WORKLOAD_REGISTRY to the origin registry root}"

repository_path="$("${jq}" -er '.images[0].target | ltrimstr("//") | split(":")[0]' "${inventory}")"
tag="$("${jq}" -er '.images[0].tag' "${inventory}")"
arguments=(--repository "${WORKLOAD_REGISTRY}/${repository_path}" --tag "${tag}")
case "${WORKLOAD_REGISTRY_INSECURE:-false}" in
  false) ;;
  true)
    if [[ ! ${WORKLOAD_REGISTRY} =~ ^(origin-registry:5000|(localhost|127\.0\.0\.1):15100)/[0-9]{12}/[a-z0-9-]+$ ]]; then
      printf '%s\n' 'Insecure workspace publication requires the local origin registry.' >&2
      exit 1
    fi
    arguments+=(--insecure)
    ;;
  *)
    printf '%s\n' 'WORKLOAD_REGISTRY_INSECURE must be true or false.' >&2
    exit 1
    ;;
esac

exec "${pusher}" "${arguments[@]}"
