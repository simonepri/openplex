#!/usr/bin/env bash
# Detect duplicated and copy-pasted code blocks across authored sources using jscpd.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

if [[ $# -gt 0 ]]; then
  jscpd "$@"
else
  jscpd src/ \
    --min-lines 25 \
    --min-tokens 80 \
    --ignore "**/*_test.*,**/test_*,**/*.test.k8s.yaml,**/*.schema.json,**/node_modules/**"
fi
