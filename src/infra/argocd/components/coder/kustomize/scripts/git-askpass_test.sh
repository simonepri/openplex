#!/usr/bin/env bash
# Tests that Git askpass helper reads single-line credential files, including Kubernetes Secret mounts.

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

# Kubernetes Secret volumes expose each key as a symlink into the ..data directory.
mkdir -p "${test_root}/mount/..2026_01_01" "${test_root}/missing"
printf '%s' mounted-token >"${test_root}/mount/..2026_01_01/git-token"
ln -s ..2026_01_01 "${test_root}/mount/..data"
ln -s ..data/git-token "${test_root}/mount/git-token"
out_mounted="$(GIT_USERNAME_FILE="${test_root}/username" \
  GIT_TOKEN_FILE="${test_root}/mount/git-token" \
  sh "${subject}" 'Password for https://git.example.invalid')"
[[ ${out_mounted} == mounted-token ]]
if GIT_USERNAME_FILE="${test_root}/username" \
  GIT_TOKEN_FILE="${test_root}/missing/git-token" \
  sh "${subject}" 'Password for https://git.example.invalid'; then
  exit 1
fi

printf 'line-one\nline-two\n' >"${test_root}/token"
! run_helper 'Password for https://git.example.invalid'
