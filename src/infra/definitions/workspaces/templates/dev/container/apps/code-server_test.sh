#!/usr/bin/env bash
# Verifies code-server launcher process detachment, environment initialization, and non-blocking startup behavior.

set -euo pipefail

subject="${1:-$(cd -- "$(dirname -- "$0")" && pwd)/code-server.sh}"
jq_bin="${2:-$(command -v jq || true)}"
test_root="$(mktemp -d)"
trap '[[ -f "$test_root/server.pid" ]] && kill "$(<"$test_root/server.pid")" 2>/dev/null || true; rm -rf -- "$test_root"' EXIT
mkdir -p "${test_root}/bin" "${test_root}/checkout"
if [[ -n ${jq_bin} && -x ${jq_bin} ]]; then
  cp "${jq_bin}" "${test_root}/bin/jq"
elif host_jq=$(command -v jq 2>/dev/null); then
  cp "${host_jq}" "${test_root}/bin/jq"
else
  printf 'Error: jq executable was not found\n' >&2
  exit 1
fi
export PATH="${test_root}/bin:${PATH}"
case "$(uname -m)" in
  x86_64)
    test_arch=amd64
    test_archive_sha256=53029be6c5781b7bca49b815fcc9a2a3fc111813ad8c9965b2c0f0d2985a0674
    ;;
  aarch64 | arm64)
    test_arch=arm64
    test_archive_sha256=0edb4b60d9c4744b2dd14b0911e3c2e6dd8c6f3c13bd58bda23ae744e59e7df1
    ;;
  *) exit 1 ;;
esac

cat >"${test_root}/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${*: -1}" == "https://github.com/coder/code-server/releases/download/v4.139.1/code-server-4.139.1-linux-$TEST_WORKSPACE_ARCH.tar.gz" ]]; then
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
if [[ "${*: -1}" == "https://open-vsx.org/api/BazelBuild/vscode-bazel/0.15.0/file/BazelBuild.vscode-bazel-0.15.0.vsix" ]]; then
  output=
  while (($#)); do
    [[ "$1" == --output ]] && output="$2"
    shift
  done
  [[ -n "$output" ]]
  printf '%s\n' fixture-extension >"$output"
  : >"$TEST_ROOT/extension-download.called"
  exit 0
fi
[[ -f "$TEST_ROOT/server.ready" ]]
EOF
cat >"${test_root}/bin/sha256sum" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  */extension.vsix)
    digest="${TEST_EXTENSION_ARCHIVE_SHA256:-03877ad9de60d080ec30f7880d90387bb2e26c88a7d194374173d15f49a7741d}"
    ;;
  *) digest="${TEST_ARCHIVE_SHA256:-$TEST_CODE_SERVER_SHA256}" ;;
esac
printf '%s  %s\n' "$digest" "$1"
EOF
cat >"${test_root}/bin/tar" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
destination=
while (($#)); do
  [[ "$1" == -C ]] && destination="$2"
  shift
done
[[ -n "$destination" ]]
prefix="$destination/code-server-4.139.1-linux-$TEST_WORKSPACE_ARCH"
mkdir -p "$prefix/bin"
cat >"$prefix/bin/code-server" <<'SERVER'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == --version ]]; then
  printf '%s\n' '[2026-09-05T03:30:47.849Z] fixture startup message'
  printf '%s fixture\n' \
    "${TEST_INSTALLED_VERSION:-4.139.1}"
  exit 0
fi
case " $* " in
  *" --list-extensions "*)
    if [[ -f "$TEST_ROOT/extension.installed" ]]; then
      printf '%s\n' 'BazelBuild.vscode-bazel@0.15.0'
    fi
    exit 0
    ;;
  *" --install-extension "*)
    printf '%s\n' "$*" >"$TEST_ROOT/extension-install.args"
    : >"$TEST_ROOT/extension.installed"
    exit 0
    ;;
esac
printf '%s\n' "$*" >"$TEST_ROOT/server.args"
printf '%s\n' "$$" >"$TEST_ROOT/server.pid"
: >"$TEST_ROOT/server.ready"
sleep 30
SERVER
chmod +x "$prefix/bin/code-server"
EOF
chmod +x "${test_root}/bin/curl" "${test_root}/bin/sha256sum" "${test_root}/bin/tar"

launcher_env=(
  "HOME=${test_root}/home"
  "KOPIA_RESTORE_SELECTOR=fixture-snapshot"
  "TEST_CODE_SERVER_SHA256=${test_archive_sha256}"
  "TEST_WORKSPACE_ARCH=${test_arch}"
  "WORKSPACE_CHECKOUT_PATH=${test_root}/checkout"
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
  printf '%s\n' 'launcher retained its caller output pipe after startup' >&2
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
[[ "$(<"${test_root}/runtime/workspace-code-server-ready")" == boot-token ]]
test -f "${test_root}/extension-download.called"
test -f "${test_root}/extension.installed"
grep -F -- '--install-extension' "${test_root}/extension-install.args" >/dev/null
grep -F -- '--extensions-dir' "${test_root}/extension-install.args" >/dev/null
jq -e '
  .["extensions.autoCheckUpdates"] == false and
  .["extensions.autoUpdate"] == false and
  .["files.watcherExclude"] == {"**/s3/**": true} and
  .["security.workspace.trust.emptyWindow"] == true and
  .["security.workspace.trust.enabled"] == false and
  .["security.workspace.trust.startupPrompt"] == "never" and
  .["workbench.activityBar.location"] == "top" and
  .["workbench.colorTheme"] == "Default Dark Modern" and
  .["workbench.startupEditor"] == "none"
' "${test_root}/home/.local/share/code-server/user-data/User/settings.json" >/dev/null
grep -F -- '--disable-workspace-trust' "${test_root}/server.args" >/dev/null
grep -F -- '--disable-update-check' "${test_root}/server.args" >/dev/null
grep -F -- '--link-protection-trusted-domains https://github.com' "${test_root}/server.args" >/dev/null
grep -F -- '--link-protection-trusted-domains https://*.github.com' "${test_root}/server.args" >/dev/null

# A same-boot Coder script retry observes the healthy owned process and returns.
start_launcher
wait_and_assert_output_closes "${launcher_reader_pid}"
kill -0 "${server_pid}"

# A downloaded archive whose content digest differs from the pin is rejected
# before extraction or execution.
kill "${server_pid}"
wait "${server_pid}" 2>/dev/null || true
rm -rf -- "${test_root}/runtime"
mkdir -p "${test_root}/runtime"
printf '%s\n' boot-token >"${test_root}/runtime/workspace-restore-ready"
rm -f -- "${test_root}/server.pid" "${test_root}/server.ready" "${test_root}/install.called"
if env "${launcher_env[@]}" \
  TEST_ARCHIVE_SHA256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  bash "${subject}" >"${test_root}/digest.stdout" 2>"${test_root}/digest.stderr"; then
  printf '%s\n' 'launcher accepted a code-server archive with a mismatched digest' >&2
  exit 1
fi
grep -Fxq 'Downloaded code-server archive did not match its pinned SHA-256.' \
  "${test_root}/digest.stderr"
test ! -e "${test_root}/server.pid"

# A startup log line before the version cannot hide a mismatched binary.
rm -rf -- "${test_root}/runtime"
mkdir -p "${test_root}/runtime"
printf '%s\n' boot-token >"${test_root}/runtime/workspace-restore-ready"
rm -f -- "${test_root}/install.called"
if env "${launcher_env[@]}" \
  TEST_INSTALLED_VERSION=4.132.0 \
  bash "${subject}" >"${test_root}/version.stdout" 2>"${test_root}/version.stderr"; then
  printf '%s\n' 'launcher accepted a mismatched code-server version' >&2
  exit 1
fi
grep -Fxq 'Installed code-server version must be 4.139.1, found 4.132.0.' \
  "${test_root}/version.stderr"
test ! -e "${test_root}/server.pid"

# Default extensions are installed only from artifacts matching their pin.
rm -rf -- "${test_root}/runtime" "${test_root}/home/.local/share/code-server/extensions"
mkdir -p "${test_root}/runtime"
printf '%s\n' boot-token >"${test_root}/runtime/workspace-restore-ready"
rm -f -- "${test_root}/extension.installed" "${test_root}/extension-install.args"
if env "${launcher_env[@]}" \
  TEST_EXTENSION_ARCHIVE_SHA256=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd \
  bash "${subject}" >"${test_root}/extension-digest.stdout" 2>"${test_root}/extension-digest.stderr"; then
  printf '%s\n' 'launcher accepted a default extension with a mismatched digest' >&2
  exit 1
fi
grep -Fxq 'Default extension BazelBuild.vscode-bazel did not match its pinned SHA-256.' \
  "${test_root}/extension-digest.stderr"
test ! -e "${test_root}/extension-install.args"
