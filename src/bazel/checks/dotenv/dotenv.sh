#!/usr/bin/env bash
# Lint tracked and untracked environment dotenv files across the workspace using dotenv-linter.

set -euo pipefail

dotenv_linter="${1:?missing dotenv-linter path}"
shift

if [[ ${dotenv_linter#/} == "${dotenv_linter}" ]]; then
  dotenv_linter="${PWD}/${dotenv_linter}"
fi

dotenv_files=("$@")
if ((${#dotenv_files[@]} == 0)); then
  workspace="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
  cd "${workspace}"

  # LINT.IfChange(dotenv_path_specs)
  pathspecs=(
    ":(glob)**/.env"
    ":(glob)**/.env.*"
    ":(glob)**/*.env"
    ":(glob)**/*.env.*"
  )
  # LINT.ThenChange(//src/bazel/rules/constants.bzl:dotenv_path_specs)
  while IFS= read -r -d '' dotenv_file; do
    dotenv_files+=("${dotenv_file}")
  done < <(git ls-files --cached --others --exclude-standard -z -- "${pathspecs[@]}" || true)
elif [[ -n ${BUILD_WORKSPACE_DIRECTORY:-} ]]; then
  cd "${BUILD_WORKSPACE_DIRECTORY}"
fi

if ((${#dotenv_files[@]} == 0)); then
  exit 0
fi

"${dotenv_linter}" check --plain --skip-updates "${dotenv_files[@]}"
