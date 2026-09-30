#!/usr/bin/env bash
# Detect duplicated and copy-pasted code blocks across authored sources using jscpd.

set -euo pipefail

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

if [[ ${CHECK_MODE:-} == affected ]]; then
  files=()
  if [[ $# -gt 0 ]]; then
    for arg; do
      if [[ -f ${arg} && ${arg} != .tmp/* ]]; then
        case "${arg}" in
          *.py | *.go | *.sh | *.ts | *.tsx | *.js | *.jsx | *.tf | *.yaml | *.yml | *.json)
            files+=("${arg}")
            ;;
          *) ;;
        esac
      fi
    done
  else
    while IFS= read -r changed; do
      if [[ -f ${changed} && ${changed} != .tmp/* ]]; then
        case "${changed}" in
          *.py | *.go | *.sh | *.ts | *.tsx | *.js | *.jsx | *.tf | *.yaml | *.yml | *.json)
            files+=("${changed}")
            ;;
          *) ;;
        esac
      fi
    done <<<"${CHANGED_ALL:-}"
  fi

  if ((${#files[@]} == 0)); then
    exit 0
  fi

  jscpd "${files[@]}" \
    --min-lines 25 \
    --min-tokens 80 \
    --ignore "**/*_test.*,**/test_*,**/*.test.k8s.yaml,**/*.schema.json,**/node_modules/**"
else
  if [[ $# -gt 0 ]]; then
    jscpd "$@"
  else
    jscpd src/ \
      --min-lines 25 \
      --min-tokens 80 \
      --ignore "**/*_test.*,**/test_*,**/*.test.k8s.yaml,**/*.schema.json,**/node_modules/**"
  fi
fi
