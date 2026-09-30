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
if [[ ! ${api_key} =~ ^[A-Za-z0-9_-]+$ ]]; then
  api_key=""
fi

if [[ -z ${api_key} && -n ${BUILDBUDDY_API_KEY:-} ]]; then
  if [[ ${BUILDBUDDY_API_KEY} =~ ^[A-Za-z0-9_-]+$ ]]; then
    api_key="${BUILDBUDDY_API_KEY}"
  fi
fi

if [[ -z ${api_key} ]]; then
  for rc in "${HOME:-}/.bazelrc" /home/buildbuddy/workspace/buildbuddy.bazelrc /etc/bazel.bazelrc; do
    if [[ -f ${rc} ]]; then
      candidate="$(grep -oE 'x-buildbuddy-api-key=[A-Za-z0-9_-]+' "${rc}" 2>/dev/null | head -n 1 | cut -d= -f2 || true)"
      if [[ -n ${candidate} && ${candidate} =~ ^[A-Za-z0-9_-]+$ ]]; then
        api_key="${candidate}"
        break
      fi
    fi
  done
fi

if [[ -z ${api_key} ]]; then
  printf '{}\n'
  exit 0
fi
printf '{"headers":{"x-buildbuddy-api-key":["%s"]}}\n' "${api_key}"
