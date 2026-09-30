#!/bin/sh
# Resolves local repository stream tags to digest-pinned container image references to defend test reproducibility against mutable image tag drift.

set -eu

repository_path="${1:?workload repository path is required}"
repository_contract=.tmp/state/origin-registry.json
test -f "${repository_contract}"

repository="$(
  jq --exit-status --raw-output --arg path "${repository_path}" \
    '.host.repositories[$path]' "${repository_contract}"
)"
registry_address="$(printf '%s' "${repository}" | cut -d/ -f1)"
registry_path="$(printf '%s' "${repository}" | cut -d/ -f2-)"
stream_tag="$(bash src/bazel/rules/oci/stream_tag.sh)"
headers="$(
  curl --fail --silent --show-error --head \
    --header 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json' \
    "http://${registry_address}/v2/${registry_path}/manifests/${stream_tag}"
)"
digest="$(
  printf '%s\n' "${headers}" | tr -d '\r' \
    | awk 'tolower($1) == "docker-content-digest:" {print $2}'
)"

digest_lines="$(printf '%s\n' "${digest}" | wc -l | tr -d '[:space:]')"
test "${digest_lines}" = 1
printf '%s\n' "${digest}" | grep -Eq '^sha256:[0-9a-f]{64}$'
printf '%s@%s\n' "${repository}" "${digest}"
