#!/usr/bin/env bash
# Test that the BuildBuddy credential helper sends the git-configured key and sends nothing without one.

set -euo pipefail

helper="${PWD}/${1:?missing credential helper path}"
repo="$(mktemp -d "${TEST_TMPDIR:-${TMPDIR:-/tmp}}/repo.XXXXXX")"
unset BUILDBUDDY_API_KEY
export HOME="${repo}"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
git init --quiet "${repo}"
cd "${repo}"

request='{"uri":"https://remote.buildbuddy.io"}'

expect() {
  local want="$1" got
  got="$("${helper}" get <<<"${request}")"
  if [[ ${got} != "${want}" ]]; then
    printf 'want %s, got %s\n' "${want}" "${got}" >&2
    exit 1
  fi
}

expect '{}'

git config buildbuddy.api-key 'not a key"'
expect '{}'

git config buildbuddy.api-key testkey123
expect '{"headers":{"x-buildbuddy-api-key":["testkey123"]}}'

if "${helper}" store <<<"${request}" 2>/dev/null; then
  printf 'expected unsupported command to fail\n' >&2
  exit 1
fi
