#!/usr/bin/env bash
# Verify that development-only tool lockfiles are excluded from shipped OCI container dependency graphs.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"
policy="${root}/src/bazel/checks/license_images/policy.json"
inventory="${root}/src/infra/images/workload-images.json"
bazel_root="${BAZEL_OUTPUT_ROOT:-${root}/.tmp/state/bazel}"

dev_locks=()
while IFS= read -r lock; do
  dev_locks+=("${lock}")
done < <(jq -r '.sourceScan.targets[] | select(.context == "dev-tool") | .path' "${policy}" || true)
if ((${#dev_locks[@]} == 0)); then
  echo 'At least one development-tool lock must be declared.' >&2
  exit 1
fi

for lock in "${dev_locks[@]}"; do
  [[ -f "${root}/${lock}" && ! -L "${root}/${lock}" ]] || {
    printf 'Development-tool lock is missing or unsafe: %s\n' "${lock}" >&2
    exit 1
  }
done

image_targets=()
while IFS= read -r target; do
  image_targets+=("${target}")
done < <(jq -r '.images[].target' "${inventory}" || true)
if ((${#image_targets[@]} == 0)); then
  echo 'No shipped OCI targets were found in the workload image inventory.' >&2
  exit 1
fi
target_set="$(printf '%s ' "${image_targets[@]}")"
lock_labels=()
for lock in "${dev_locks[@]}"; do
  directory="$(dirname "${lock}")"
  [[ ${directory} == "." ]] && directory=""
  lock_labels+=("//${directory}:$(basename "${lock}")")
done
# Bazel's own set algebra answers the membership question in one query.
reached="$(
  bazel --output_user_root="${bazel_root}" query \
    "deps(set(${target_set})) intersect set(${lock_labels[*]})" --output=label
)"
if [[ -n ${reached} ]]; then
  printf 'Development-tool lock reaches a shipped OCI target:\n%s\n' "${reached}" >&2
  exit 1
fi
