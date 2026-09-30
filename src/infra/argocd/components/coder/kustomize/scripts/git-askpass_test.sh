#!/usr/bin/env bash
# Tests that Git askpass helper reads only authorized, single-line credential files.

# shellcheck disable=SC2310
set -euo pipefail

subject="${1:?Git askpass path is required}"
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT
printf '%s' operator >"${test_root}/username"
printf '%s' secret-token >"${test_root}/token"

run_helper() {
  GIT_USERNAME_FILE="${test_root}/username" \
    GIT_TOKEN_FILE="${test_root}/token" \
    sh "${subject}" "$1"
}

out_user="$(run_helper 'Username for https://git.example.invalid')"
[[ ${out_user} == operator ]]
out_pass="$(run_helper 'Password for https://git.example.invalid')"
[[ ${out_pass} == secret-token ]]
run_helper 'unsupported prompt' && exit 1

ln -s "${test_root}/token" "${test_root}/token-link"
if GIT_USERNAME_FILE="${test_root}/username" \
  GIT_TOKEN_FILE="${test_root}/token-link" \
  sh "${subject}" 'Password for https://git.example.invalid'; then
  exit 1
fi

printf 'line-one\nline-two\n' >"${test_root}/token"
! run_helper 'Password for https://git.example.invalid'
