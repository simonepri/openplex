#!/usr/bin/env bash
# Calculate and enforce cyclomatic complexity across Go functions using gocyclo.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

threshold="${CYCLO_OVER:-30}"

if [[ $# -gt 0 ]]; then
  gocyclo "$@"
else
  gocyclo -over "${threshold}" -ignore "_test\.go" src/
fi
