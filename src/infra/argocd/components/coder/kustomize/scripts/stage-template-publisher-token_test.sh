#!/usr/bin/env bash
# Tests validation and security constraints of the publisher token staging script.

set -euo pipefail

subject="${1:?publisher-token staging path is required}"
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT
printf '%s' scoped-token >"${test_root}/source"

CODER_TEMPLATE_PUBLISHER_TOKEN_SOURCE="${test_root}/source" \
  CODER_SESSION_TOKEN_FILE="${test_root}/staged" \
  sh "${subject}"
[[ "$(<"${test_root}/staged")" == scoped-token ]]
file_perm="$(stat -c '%a' "${test_root}/staged" 2>/dev/null || stat -f '%Lp' "${test_root}/staged")"
[[ ${file_perm} == 600 ]]

rm "${test_root}/staged"
ln -s "${test_root}/source" "${test_root}/source-link"
if CODER_TEMPLATE_PUBLISHER_TOKEN_SOURCE="${test_root}/source-link" \
  CODER_SESSION_TOKEN_FILE="${test_root}/staged" \
  sh "${subject}"; then
  exit 1
fi
[[ ! -e "${test_root}/staged" ]]

: >"${test_root}/empty"
if CODER_TEMPLATE_PUBLISHER_TOKEN_SOURCE="${test_root}/empty" \
  CODER_SESSION_TOKEN_FILE="${test_root}/staged" \
  sh "${subject}"; then
  exit 1
fi
