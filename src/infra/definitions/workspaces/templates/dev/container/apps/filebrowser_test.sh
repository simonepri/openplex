#!/usr/bin/env bash
# shellcheck disable=SC2310
# Tests filebrowser daemon download, mock execution, ready token propagation, and process detachment.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/filebrowser.sh}"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/filebrowser-test.XXXXXX")"
cleanup() {
  if [[ -f "${test_root}/server.pid" ]]; then
    kill -9 "$(<"${test_root}/server.pid")" 2>/dev/null || true
  fi
  rm -rf -- "${test_root}"
}
trap cleanup EXIT

mkdir -p "${test_root}/bin" "${test_root}/checkout" "${test_root}/home"

case "$(uname -m)" in
  x86_64)
    test_arch=amd64
    test_archive_sha256=b14db2bb8033caa3f80205eb6578b2ed0744ebd9e716b790bc4a9703ce909e88
    ;;
  aarch64 | arm64)
    test_arch=arm64
    test_archive_sha256=c55b3450b6ac07ef73b5b49f0b2955a8e755100b71af1df73f9e5d25b12fd27a
    ;;
  *) exit 1 ;;
esac

cat >"${test_root}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${*: -1}" == "https://github.com/filebrowser/filebrowser/releases/download/v2.63.23/linux-$TEST_WORKSPACE_ARCH-filebrowser.tar.gz" ]]; then
  output=
  while (($#)); do
    [[ "$1" == --output ]] && output="$2"
    shift
  done
  [[ -n "$output" ]]
  : >"$output"
  : >"$TEST_ROOT/install.called"
  exit 0
fi
[[ -f "$TEST_ROOT/server.ready" ]]
EOF

cat >"${test_root}/bin/sha256sum" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s  %s\n' "$TEST_FILEBROWSER_SHA256" "$1"
EOF

cat >"${test_root}/bin/tar" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
target_dir=
while (($#)); do
  [[ "$1" == -C ]] && target_dir="$2"
  shift
done
mkdir -p "${target_dir}"
cat >"${target_dir}/filebrowser" <<'SERVER_EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "config" ]] || [[ "${1:-}" == "users" ]] || [[ "${1:-}" == "rules" ]]; then
  exit 0
fi
printf '%s\n' "$*" >"$TEST_ROOT/server.args"
printf '%s\n' "$$" >"$TEST_ROOT/server.pid"
: >"$TEST_ROOT/server.ready"
while true; do
  sleep 1
done
SERVER_EOF
chmod +x "${target_dir}/filebrowser"
EOF

chmod +x "${test_root}/bin/curl" "${test_root}/bin/sha256sum" "${test_root}/bin/tar"

launcher_env=(
  "HOME=${test_root}/home"
  "KOPIA_RESTORE_SELECTOR=fixture-snapshot"
  "TEST_FILEBROWSER_SHA256=${test_archive_sha256}"
  "TEST_ROOT=${test_root}"
  "TEST_WORKSPACE_ARCH=${test_arch}"
  "WORKSPACE_BOOT_TOKEN=boot-token"
  "WORKSPACE_CHECKOUT_PATH=${test_root}/checkout"
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
test ! -e "${test_root}/install.called"
printf '%s\n' boot-token >"${test_root}/runtime/workspace-restore-ready"
wait_and_assert_output_closes "${launcher_reader_pid}"
server_pid="$(<"${test_root}/server.pid")"
kill -0 "${server_pid}"

[[ -f "${test_root}/install.called" ]]
[[ -f "${test_root}/server.ready" ]]
[[ -f "${test_root}/server.pid" ]]
[[ -f "${test_root}/runtime/workspace-filebrowser-ready" ]]
[[ "$(<"${test_root}/runtime/workspace-filebrowser-ready")" == "boot-token" ]]
grep -F -- '--port 13339' "${test_root}/server.args" >/dev/null
grep -F -- '--root /fs' "${test_root}/server.args" >/dev/null

printf 'All filebrowser tests passed.\n'
