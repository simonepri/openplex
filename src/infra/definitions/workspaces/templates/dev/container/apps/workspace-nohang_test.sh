#!/usr/bin/env bash
# Verifies the nohang startup script returns while its daemon remains alive and reports startup failures.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-nohang.sh}"
test_root="$(mktemp -d)"
cleanup() {
  if [[ -r ${test_root}/workspace-nohang.pid ]]; then
    kill "$(<"${test_root}/workspace-nohang.pid")" 2>/dev/null || true
  fi
  rm -rf -- "${test_root}"
}
trap cleanup EXIT
mkdir -p "${test_root}/bin"
cat >"${test_root}/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$TEST_ROOT/python.args"
if [[ ${FAIL_START:-false} == true ]]; then
  exit 7
fi
while true; do sleep 1; done
EOF
chmod +x "${test_root}/bin/python3"

if PATH="${test_root}/bin:${PATH}" bash "${subject}" "${test_root}/nonexistent.py" 2>/dev/null; then
  printf '%s\n' 'expected missing python file to fail startup' >&2
  exit 1
fi

touch "${test_root}/workspace_nohang.py"
TEST_ROOT="${test_root}" XDG_RUNTIME_DIR="${test_root}" PATH="${test_root}/bin:${PATH}" \
  bash "${subject}" "${test_root}/workspace_nohang.py"
pid="$(<"${test_root}/workspace-nohang.pid")"
kill -0 "${pid}"
[[ $(<"${test_root}/python.args") == "${test_root}/workspace_nohang.py" ]]

TEST_ROOT="${test_root}" XDG_RUNTIME_DIR="${test_root}" PATH="${test_root}/bin:${PATH}" \
  bash "${subject}" "${test_root}/workspace_nohang.py"
[[ $(<"${test_root}/workspace-nohang.pid") == "${pid}" ]]
invocations="$(wc -l <"${test_root}/python.args")"
[[ ${invocations} -eq 1 ]]
kill "${pid}"
rm "${test_root}/workspace-nohang.pid"

if FAIL_START=true TEST_ROOT="${test_root}" XDG_RUNTIME_DIR="${test_root}" PATH="${test_root}/bin:${PATH}" \
  bash "${subject}" "${test_root}/workspace_nohang.py" 2>/dev/null; then
  printf '%s\n' 'expected daemon failure to fail startup' >&2
  exit 1
fi
[[ ! -e ${test_root}/workspace-nohang.pid ]]
