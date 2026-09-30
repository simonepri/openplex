#!/usr/bin/env bash
# Tests Zasper launcher process detachment, port binding verification, and background execution lifecycle.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/workspace-zasper.sh}"
test_root="$(mktemp -d)"
trap '[[ -f "$test_root/server.pid" ]] && kill "$(<"$test_root/server.pid")" 2>/dev/null || true; rm -rf -- "$test_root"' EXIT
mkdir -p "${test_root}/bin"

cat >"${test_root}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ -f "$TEST_ROOT/server.ready" ]]
EOF

cat >"${test_root}/bin/zasper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >"$TEST_ROOT/server.args"
printf '%s\n' "$$" >"$TEST_ROOT/server.pid"
printf '%s\n' "$ZASPER_ACCESS_TOKEN" >"$TEST_ROOT/server.token"
: >"$TEST_ROOT/server.ready"
sleep 30
EOF
chmod +x "${test_root}/bin/curl" "${test_root}/bin/zasper"

launcher_env=(
  "HOME=${test_root}/home"
  "KOPIA_RESTORE_SELECTOR=fixture-snapshot"
  "WORKSPACE_BOOT_TOKEN=boot-token"
  "TEST_ROOT=${test_root}"
  "XDG_RUNTIME_DIR=${test_root}/runtime"
  "PATH=${test_root}/bin:${PATH}"
)

wait_and_assert_output_closes() {
  local reader_pid=$1

  for _ in {1..50}; do
    kill -0 "${reader_pid}" 2>/dev/null || {
      wait "${reader_pid}"
      return
    }
    sleep 0.1
  done
  kill "${reader_pid}" 2>/dev/null || true
  wait "${reader_pid}" 2>/dev/null || true
  printf '%s\n' "launcher retained its caller output pipe after startup" >&2
  exit 1
}

start_launcher() {
  { env "${launcher_env[@]}" bash "${subject}" 2>&1; } \
    | cat >>"${test_root}/launcher.out" &
  launcher_reader_pid=$!
}

mkdir -p "${test_root}/runtime"
printf '%s\n' stale-boot >"${test_root}/runtime/workspace-restore-ready"
start_launcher
sleep 0.2
kill -0 "${launcher_reader_pid}"
test ! -e "${test_root}/server.pid"
printf '%s\n' boot-token >"${test_root}/runtime/workspace-restore-ready"
wait_and_assert_output_closes "${launcher_reader_pid}"
server_pid="$(<"${test_root}/server.pid")"
kill -0 "${server_pid}"
[[ "$(<"${test_root}/runtime/workspace-zasper-ready")" == boot-token ]]
[[ "$(<"${test_root}/server.token")" == boot-token ]]
grep -F -- '--cwd /fs' "${test_root}/server.args" >/dev/null
grep -F -- '--host 0.0.0.0' "${test_root}/server.args" >/dev/null
grep -F -- '--port :8048' "${test_root}/server.args" >/dev/null
grep -F -- '--no-browser' "${test_root}/server.args" >/dev/null
grep -F -- '--tracking=false' "${test_root}/server.args" >/dev/null

# A same-boot Coder script retry observes the healthy owned process and returns.
start_launcher
wait_and_assert_output_closes "${launcher_reader_pid}"
kill -0 "${server_pid}"
