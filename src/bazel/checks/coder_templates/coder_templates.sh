#!/usr/bin/env bash
# Validate initialization and publication entry points for all Coder workspace templates.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"
templates_root="${root}/src/infra/definitions/workspaces/templates"

if [[ ! -d ${templates_root} ]]; then
  exit 0
fi

while IFS= read -r -d '' publication_entrypoint; do
  test -x "${publication_entrypoint}"
  "${publication_entrypoint}" --check
done < <(find "${templates_root}" -mindepth 2 -maxdepth 2 -type f -name publish.sh -print0 || true)

# Verify all declared template image targets exist in the Bazel graph.
bazel_root="${BAZEL_OUTPUT_ROOT:-${root}/.tmp/state/bazel}"
while IFS= read -r -d '' images_file; do
  targets="$(jq -r '[.images[].target] | join(" ")' "${images_file}")"
  if [[ -n ${targets} ]]; then
    bazel --output_user_root="${bazel_root}" query "set(${targets})" --output=label >/dev/null
  fi
done < <(find "${templates_root}" -mindepth 2 -maxdepth 2 -type f -name workspace-images.json -print0 || true)
