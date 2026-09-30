#!/bin/sh
# Tests username validation rules, sanitization edge cases, and NSS database updates in workspace-identity.sh.

set -eu

subject=${1:?identity script is required}
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT

sh "${subject}" simonepri_ldap "${test_dir}/valid"
grep -Fx 'simonepri_ldap:x:1000:1000:Workspace:/home/coder:/usr/bin/zsh' "${test_dir}/valid/passwd"
grep -Fx 'simonepri_ldap:x:1000:' "${test_dir}/valid/group"

if sh "${subject}" 'Alice.Admin' "${test_dir}/invalid" 2>/dev/null; then
  printf '%s\n' 'Unsafe mixed-case/dotted login name was accepted.' >&2
  exit 1
fi
test ! -e "${test_dir}/invalid/passwd"
test ! -e "${test_dir}/invalid/group"
