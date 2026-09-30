#!/usr/bin/env bash
# Verifies argument translation and CA file defaulting for the in-pod Coder CLI wrapper.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/coder-cli.sh}"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

# 1. Test binary missing failure
if (
  unset CODER_CLIENT_TLS_CA_FILE
  bash "${subject}"
) 2>"${test_root}/err.log"; then
  echo "Expected coder-cli to fail when binary is missing" >&2
  exit 1
fi
grep -F "coder: binary not found in workspace" "${test_root}/err.log" >/dev/null

# 2. Setup mock coder binary in test_root/coder.mock/coder
mock_coder_dir="${test_root}/coder.mock"
mkdir -p "${mock_coder_dir}"
mock_bin="${mock_coder_dir}/coder"
cat >"${mock_bin}" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "CA_FILE=${CODER_CLIENT_TLS_CA_FILE:-none}"
for arg in "$@"; do
  printf 'ARG=%s\n' "${arg}"
done
EOF
chmod +x "${mock_bin}"

# 3. Test CA bundle defaulting and 'self' replacement
test_script="${test_root}/coder-test.sh"
sed "s|/tmp/coder\.\*/coder|${test_root}/coder\.\*/coder|" "${subject}" >"${test_script}"
chmod +x "${test_script}"

output="$(
  unset CODER_CLIENT_TLS_CA_FILE
  export CODER_WORKSPACE_NAME="my-workspace"
  bash "${test_script}" restart self --extra
)"

[[ ${output} == *"ARG=restart"* ]]
[[ ${output} == *"ARG=my-workspace"* ]]
[[ ${output} == *"ARG=--extra"* ]]

printf 'All coder-cli tests passed.\n'
