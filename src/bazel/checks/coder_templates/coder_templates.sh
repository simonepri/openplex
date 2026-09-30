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

# Verify all declared template image targets exist in the Bazel graph. A label
# //:check found in its graph query exists (see graph_facts.py); any other
# label goes to Bazel, which reports the missing target.
bazel_root="${BAZEL_OUTPUT_ROOT:-${root}/.tmp/state/bazel}"
known_labels="${CHECK_FACTS_DIR:-}/labels"
while IFS= read -r -d '' images_file; do
  targets="$(jq -r '[.images[].target] | join(" ")' "${images_file}")"
  all_known=0
  if [[ -n ${CHECK_FACTS_DIR:-} && -f ${known_labels} ]]; then
    all_known=1
    for target in ${targets}; do
      grep -Fxq -- "${target}" "${known_labels}" || all_known=0
    done
  fi
  if [[ -n ${targets} && ${all_known} == 0 ]]; then
    # shellcheck disable=SC2086
    bazel --output_user_root="${bazel_root}" query ${BAZEL_CONFIG_FLAGS:-} "set(${targets})" --output=label >/dev/null
  fi
done < <(find "${templates_root}" -mindepth 2 -maxdepth 2 -type f -name workspace-images.json -print0 || true)
