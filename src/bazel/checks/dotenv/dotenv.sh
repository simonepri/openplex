#!/usr/bin/env bash
# Lint tracked and untracked environment dotenv files across the workspace using dotenv-linter.

set -euo pipefail

dotenv_linter="${1:?missing dotenv-linter path}"
shift

if [[ ${dotenv_linter#/} == "${dotenv_linter}" ]]; then
  dotenv_linter="${PWD}/${dotenv_linter}"
fi

if (($# > 0)); then
  if [[ -n ${BUILD_WORKSPACE_DIRECTORY:-} ]]; then
    cd "${BUILD_WORKSPACE_DIRECTORY}"
  fi
  for file in "$@"; do
    [[ -n ${file} ]] && printf '%s\0' "${file}"
  done | xargs -0 -r "${dotenv_linter}" check --plain --skip-updates
  exit 0
fi

workspace="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${workspace}"

pathspecs=(
  ":(glob)**/.env"
  ":(glob)**/.env.*"
  ":(glob)**/*.env"
  ":(glob)**/*.env.*"
)

stream_dotenv_files() {
  if [[ ${CHECK_MODE:-} == "affected" ]]; then
    if [[ -n ${CHANGED_ALL+x} ]]; then
      while IFS= read -r file; do
        [[ -z ${file} ]] && continue
        local base="${file##*/}"
        if [[ ${base} == .env || ${base} == .env.* || ${base} == *.env || ${base} == *.env.* ]]; then
          if [[ -f ${file} ]]; then
            printf '%s\0' "${file}"
          fi
        fi
      done <<<"${CHANGED_ALL}"
    else
      local diff_base="${DIFF_BASE:-$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || echo HEAD~1)}"
      {
        git diff --name-only --diff-filter=d -z "${diff_base}" -- "${pathspecs[@]}" ':(exclude)bazel-*' ':(exclude).tmp*' ':(exclude)_tmp*' 2>/dev/null || true
        git ls-files --others --exclude-standard -z -- "${pathspecs[@]}" ':(exclude)bazel-*' ':(exclude).tmp*' ':(exclude)_tmp*' 2>/dev/null || true
      } | while IFS= read -r -d '' file; do
        [[ -f ${file} ]] && printf '%s\0' "${file}"
      done
    fi
  else
    { git ls-files --cached --others --exclude-standard -z -- "${pathspecs[@]}" ':(exclude)bazel-*' ':(exclude).tmp*' ':(exclude)_tmp*' 2>/dev/null || true; } \
      | while IFS= read -r -d '' file; do
        [[ -f ${file} ]] && printf '%s\0' "${file}"
      done
  fi
}

stream_dotenv_files | xargs -0 -r "${dotenv_linter}" check --plain --skip-updates
