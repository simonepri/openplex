#!/usr/bin/env bash
# Answer Bazel credential helper requests with the BuildBuddy API key that `bb login` stores in git config.

set -euo pipefail

command="${1:-}"
# Bazel writes the request to stdin; drain it so the write never fails.
cat >/dev/null

if [[ ${command} != get ]]; then
  printf 'unsupported credential helper command: %s\n' "${command}" >&2
  exit 1
fi

# Bazel runs the helper from the workspace root. A missing key, or no git,
# yields an empty response so Bazel connects without the header.
api_key="$(git config --get buildbuddy.api-key 2>/dev/null || true)"
if [[ ! ${api_key} =~ ^[A-Za-z0-9]+$ ]]; then
  printf '{}\n'
  exit 0
fi
printf '{"headers":{"x-buildbuddy-api-key":["%s"]}}\n' "${api_key}"
