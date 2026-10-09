#!/usr/bin/env bash
# Validate consistency of LINT.IfChange and LINT.ThenChange block annotations across the repository.

set -euo pipefail

orig_pwd="${PWD}"
tool="${1:?missing ifttt-lint path}"
shift || true

if [[ ${tool#/} == "${tool}" ]]; then
  if [[ -n ${TEST_SRCDIR:-} && -f ${TEST_SRCDIR}/_main/${tool} ]]; then
    tool="${TEST_SRCDIR}/_main/${tool}"
  elif [[ -f ${orig_pwd}/${tool} ]]; then
    tool="${orig_pwd}/${tool}"
  fi
fi

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

if [[ ${NO_IFTTT:-0} == "1" || ${NO_IFTTT:-} =~ ^(true|TRUE|yes)$ ]]; then
  echo "INFO: IFTTT check skipped via NO_IFTTT."
  exit 0
fi

git ls-files -z --cached --others --exclude-standard -- ':(exclude)bazel-*' ':(exclude).tmp*' ':(exclude)_tmp*' \
  | while IFS= read -r -d '' path; do
    [[ -f ${path} ]] && printf '%s\0' "${path}"
  done \
  | xargs -0 "${tool}" --format plain
