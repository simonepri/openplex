#!/usr/bin/env bash
# Calculate and enforce cyclomatic complexity across Go functions using gocyclo.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

threshold="${CYCLO_OVER:-30}"

if [[ $# -gt 0 ]]; then
  gocyclo "$@"
elif [[ ${CHECK_MODE:-} == affected ]]; then
  go_files=()
  while IFS= read -r file; do
    [[ -n ${file} ]] || continue
    if [[ ${file} == *.go && ${file} != *_test.go && -f ${file} ]]; then
      go_files+=("${file}")
    fi
  done <<<"${CHANGED_ALL:-}"

  if ((${#go_files[@]} == 0)); then
    exit 0
  fi

  printf '%s\0' "${go_files[@]}" | xargs -0 gocyclo -over "${threshold}" -ignore "_test\.go"
else
  gocyclo -over "${threshold}" -ignore "_test\.go" src/
fi
