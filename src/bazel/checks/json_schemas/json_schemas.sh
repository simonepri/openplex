#!/usr/bin/env bash
# Validate repository JSON schemas against their draft metaschemas using check-jsonschema.

set -euo pipefail

check_jsonschema="${1:?missing check-jsonschema path}"
shift

if [[ ${check_jsonschema#/} == "${check_jsonschema}" ]]; then
  check_jsonschema="${PWD}/${check_jsonschema}"
fi

schema_files=("$@")
if ((${#schema_files[@]} == 0)); then
  workspace="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
  cd "${workspace}"

  pathspecs=(
    ":(glob)**/*.schema.json"
  )
  while IFS= read -r -d '' schema_file; do
    schema_files+=("${schema_file}")
  done < <(git ls-files --cached --others --exclude-standard -z -- "${pathspecs[@]}" || true)
elif [[ -n ${BUILD_WORKSPACE_DIRECTORY:-} ]]; then
  cd "${BUILD_WORKSPACE_DIRECTORY}"
fi

if ((${#schema_files[@]} == 0)); then
  exit 0
fi

"${check_jsonschema}" --check-metaschema "${schema_files[@]}"
