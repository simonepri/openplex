#!/usr/bin/env bash
# Tests SSH and token credential wiring for Coder template source checkout.

set -euo pipefail

subject="${1:?prepare-template-source path is required}"
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT

mkdir -p "${test_root}/bin" "${test_root}/state" "${test_root}/source"

# Mock git binary to capture environment and invocations.
cat >"${test_root}/bin/git" <<'EOF'
#!/bin/sh
printf '%s: %s\n' "$*" "${GIT_SSH_COMMAND:-none}" >>"${MOCK_CALLS}"
if [ "${1:-}" = "fetch" ] || [ "${2:-}" = "fetch" ]; then
  exit 0
fi
EOF
chmod +x "${test_root}/bin/git"

# Test 1: SSH key authentication path.
printf '%s\n' 'dummy-ssh-private-key' >"${test_root}/deploy-key"
export MOCK_CALLS="${test_root}/calls-ssh"
: >"${MOCK_CALLS}"

PATH="${test_root}/bin:${PATH}" \
  STATE_DIR="${test_root}/state" \
  SOURCE_REPOSITORY="git@github.com:example/repo.git" \
  SOURCE_REVISION="main" \
  GIT_SSH_KEY_FILE="${test_root}/deploy-key" \
  sh "${subject}"

[[ -f "${test_root}/state/.ssh/id_rsa" ]]
[[ "$(<"${test_root}/state/.ssh/id_rsa")" == 'dummy-ssh-private-key' ]]
file_perm="$(stat -c '%a' "${test_root}/state/.ssh/id_rsa" 2>/dev/null || stat -f '%Lp' "${test_root}/state/.ssh/id_rsa")"
[[ ${file_perm} == 600 ]]
grep -q "ssh -i ${test_root}/state/.ssh/id_rsa" "${MOCK_CALLS}"

# Test 2: Username and token authentication path.
printf '%s' 'git-user' >"${test_root}/username"
printf '%s' 'git-token' >"${test_root}/token"
rm -rf "${test_root}/state/.ssh"
export MOCK_CALLS="${test_root}/calls-token"
: >"${MOCK_CALLS}"

PATH="${test_root}/bin:${PATH}" \
  STATE_DIR="${test_root}/state" \
  SOURCE_REPOSITORY="https://github.com/example/repo.git" \
  SOURCE_REVISION="main" \
  GIT_USERNAME_FILE="${test_root}/username" \
  GIT_TOKEN_FILE="${test_root}/token" \
  sh "${subject}"

[[ ! -e "${test_root}/state/.ssh" ]]
grep -q "fetch --depth=1 origin main: none" "${MOCK_CALLS}"
