#!/usr/bin/env bash
# Test that the BuildBuddy credential helper sends the git-configured key and fails when missing.

set -euo pipefail

helper="${PWD}/${1:?missing credential helper path}"
repo="$(mktemp -d "${TEST_TMPDIR:-${TMPDIR:-/tmp}}/repo.XXXXXX")"
unset BUILDBUDDY_API_KEY
unset BUILDBUDDY_INVOCATION_ID
export HOME="${repo}"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
git init --quiet "${repo}"
cd "${repo}"

request='{"uri":"https://remote.buildbuddy.io"}'
# shellcheck disable=SC2016
missing_msg='BuildBuddy API key missing: run `mise run login` in the depot checkout.'

expect() {
  local want="$1" got
  got="$("${helper}" get <<<"${request}")"
  if [[ ${got} != "${want}" ]]; then
    printf 'want %s, got %s\n' "${want}" "${got}" >&2
    exit 1
  fi
}

expect_missing() {
  local stdout stderr status=0
  stdout="$("${helper}" get <<<"${request}" 2>"${repo}/stderr")" || status=$?
  stderr="$(cat "${repo}/stderr")"
  if [[ ${status} -eq 0 ]]; then
    printf 'expected helper to fail for missing key, but exited 0\n' >&2
    exit 1
  fi
  if [[ -n ${stdout} ]]; then
    printf 'want empty stdout, got %s\n' "${stdout}" >&2
    exit 1
  fi
  if [[ ${stderr} != "${missing_msg}" ]]; then
    printf 'want stderr %s, got %s\n' "${missing_msg}" "${stderr}" >&2
    exit 1
  fi
}

expect_missing

git config buildbuddy.api-key 'not a key"'
expect_missing

git config buildbuddy.api-key testkey123
expect '{"headers":{"x-buildbuddy-api-key":["testkey123"]}}'

# BuildBuddy CI detection suppresses key to avoid duplicate headers with CI's --remote_header
BUILDBUDDY_INVOCATION_ID="test-inv-id" expect '{}'
unset BUILDBUDDY_INVOCATION_ID

# Non-BuildBuddy CI environment (e.g. GitHub Actions) still sends the configured key
CI="true" expect '{"headers":{"x-buildbuddy-api-key":["testkey123"]}}'

# An existing x-buildbuddy-api-key in buildbuddy.bazelrc suppresses helper header
printf 'build --remote_header=x-buildbuddy-api-key=rc-key\n' >buildbuddy.bazelrc
expect '{}'
rm -f buildbuddy.bazelrc

# Environment variable fallback when git config is missing
git config --unset buildbuddy.api-key
BUILDBUDDY_API_KEY="envkey456" expect '{"headers":{"x-buildbuddy-api-key":["envkey456"]}}'
unset BUILDBUDDY_API_KEY

if "${helper}" store <<<"${request}" 2>/dev/null; then
  printf 'expected unsupported command to fail\n' >&2
  exit 1
fi
