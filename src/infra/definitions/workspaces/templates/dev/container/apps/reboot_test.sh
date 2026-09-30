#!/usr/bin/env bash
# Verifies workspace reboot invocation, name validation, and argument passthrough.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/reboot.sh}"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

# 1. Test missing CODER_WORKSPACE_NAME
if (
  unset CODER_WORKSPACE_NAME
  bash "${subject}"
) 2>"${test_root}/err.log"; then
  echo "Expected reboot to fail when CODER_WORKSPACE_NAME is unset" >&2
  exit 1
fi
grep -F "reboot: CODER_WORKSPACE_NAME is not set" "${test_root}/err.log" >/dev/null

# 2. Test missing coder binary
bash_bin="$(command -v bash)"
bash_dir="$(dirname -- "${bash_bin}")"
if (
  CODER_WORKSPACE_NAME="my-workspace" \
    PATH="${bash_dir}:/usr/bin:/bin" \
    bash "${subject}"
) 2>"${test_root}/err2.log"; then
  echo "Expected reboot to fail when coder is missing" >&2
  exit 1
fi
grep -F "reboot: coder binary not found in workspace" "${test_root}/err2.log" >/dev/null

# 3. Test successful reboot execution with mock coder
mock_bin_dir="${test_root}/bin"
mkdir -p "${mock_bin_dir}"
cat >"${mock_bin_dir}/coder" <<'EOF'
#!/usr/bin/env bash
printf 'CODER_INVOKED: %s\n' "$*"
EOF
chmod +x "${mock_bin_dir}/coder"

output="$(
  CODER_WORKSPACE_NAME="prod-ws" \
    PATH="${mock_bin_dir}:${bash_dir}:/usr/bin:/bin:${PATH}" \
    bash "${subject}" --flag-one
)"

[[ ${output} == *"Restarting workspace prod-ws..."* ]]
[[ ${output} == *"CODER_INVOKED: restart prod-ws -y --flag-one"* ]]

printf 'All reboot tests passed.\n'
