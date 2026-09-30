#!/usr/bin/env bash
# Validate repository JSON schemas against their draft metaschemas using check-jsonschema.

set -euo pipefail

check_jsonschema="${1:?missing check-jsonschema path}"
shift

if [[ ${check_jsonschema#/} == "${check_jsonschema}" ]]; then
  check_jsonschema="${PWD}/${check_jsonschema}"
fi

if (($# > 0)); then
  if [[ -n ${BUILD_WORKSPACE_DIRECTORY:-} ]]; then
    cd "${BUILD_WORKSPACE_DIRECTORY}"
  fi
  for file in "$@"; do
    [[ -n ${file} ]] && printf '%s\0' "${file}"
  done | xargs -0 -r "${check_jsonschema}" --check-metaschema
  exit 0
fi

workspace="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${workspace}"

pathspecs=(
  ":(glob)**/*.schema.json"
)

stream_schema_files() {
  if [[ ${CHECK_MODE:-} == "affected" ]]; then
    if [[ -n ${CHANGED_ALL+x} ]]; then
      while IFS= read -r file; do
        [[ -z ${file} ]] && continue
        if [[ ${file} == *.schema.json && -f ${file} ]]; then
          printf '%s\0' "${file}"
        fi
      done <<<"${CHANGED_ALL}"
    else
      local diff_base="${DIFF_BASE:-$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || echo HEAD~1)}"
      {
        git diff --name-only --diff-filter=d -z "${diff_base}" -- "${pathspecs[@]}" 2>/dev/null || true
        git ls-files --others --exclude-standard -z -- "${pathspecs[@]}" 2>/dev/null || true
      } | while IFS= read -r -d '' file; do
        [[ -f ${file} ]] && printf '%s\0' "${file}"
      done
    fi
  else
    { git ls-files --cached --others --exclude-standard -z -- "${pathspecs[@]}" 2>/dev/null || true; } \
      | while IFS= read -r -d '' file; do
        [[ -f ${file} ]] && printf '%s\0' "${file}"
      done
  fi
}

stream_schema_files | xargs -0 -r "${check_jsonschema}" --check-metaschema
