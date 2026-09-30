#!/usr/bin/env bash
# Validate consistency of LINT.IfChange and LINT.ThenChange block annotations across the repository.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

if [[ $# -gt 0 ]]; then
  exec ifttt-lint "$@"
fi

git ls-files -z --cached --others --exclude-standard \
  | while IFS= read -r -d '' path; do
    [[ -f ${path} ]] && printf '%s\0' "${path}"
  done \
  | xargs -0 ifttt-lint --format plain
