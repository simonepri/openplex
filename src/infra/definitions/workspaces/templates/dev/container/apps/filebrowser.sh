#!/usr/bin/env bash
# shellcheck disable=SC2310
# Installs and launches the File Browser web manager on loopback for authenticated Coder application proxy access.

set -euo pipefail

: "${WORKSPACE_CHECKOUT_PATH:?workspace checkout path was not injected}"
: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"
: "${HOME:?workspace home was not injected}"

filebrowser_port=13339
filebrowser_version=2.63.23
filebrowser_user="ops"
case "$(uname -m)" in
  x86_64)
    workspace_arch=amd64
    filebrowser_sha256=b14db2bb8033caa3f80205eb6578b2ed0744ebd9e716b790bc4a9703ce909e88
    ;;
  aarch64 | arm64)
    workspace_arch=arm64
    filebrowser_sha256=c55b3450b6ac07ef73b5b49f0b2955a8e755100b71af1df73f9e5d25b12fd27a
    ;;
  *)
    printf '%s\n' 'File Browser supports only amd64 and arm64 workspaces.' >&2
    exit 1
    ;;
esac

wait_for_boot_marker() {
  local file=$1

  until [[ -f ${file} ]] \
    && [[ "$(<"${file}")" == "${WORKSPACE_BOOT_TOKEN}" ]]; do
    sleep 1
  done
}

runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
mounts_ready_file="${runtime_dir}/workspace-mounts-ready"
restore_ready_file="${runtime_dir}/workspace-restore-ready"
setup_ready_file="${runtime_dir}/workspace-setup-ready"
ready_file="${runtime_dir}/workspace-filebrowser-ready"
if [[ -f ${mounts_ready_file} ]]; then
  wait_for_boot_marker "${mounts_ready_file}"
fi
if [[ -n ${KOPIA_RESTORE_SELECTOR:-} && ${KOPIA_RESTORE_SELECTOR} != "__start-fresh__" ]]; then
  wait_for_boot_marker "${restore_ready_file}"
fi
if [[ -f ${setup_ready_file} ]]; then
  wait_for_boot_marker "${setup_ready_file}"
fi
rm -f -- "${ready_file}"

write_ready_marker() {
  local temporary

  temporary="$(mktemp "${ready_file}.XXXXXX")"
  printf '%s\n' "${WORKSPACE_BOOT_TOKEN}" >"${temporary}"
  mv -f -- "${temporary}" "${ready_file}"
}

data_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/filebrowser"
database_path="${data_dir}/filebrowser.db"
branding_dir="${data_dir}/branding"
artifact="linux-${workspace_arch}-filebrowser"
install_prefix="${runtime_dir}/${artifact}"
server_binary="${install_prefix}/filebrowser"
state_file="${runtime_dir}/filebrowser-launch.state"
log_file="${runtime_dir}/filebrowser.log"
if [[ -L ${HOME} && ! -d ${HOME} ]]; then
  target_home=""
  target_home="$(readlink -f "${HOME}" 2>/dev/null || readlink "${HOME}" || true)"
  if [[ -n ${target_home} ]]; then
    mkdir -p "${target_home}"
  fi
fi
mkdir -p "${HOME}"
mkdir -p "${runtime_dir}" "${data_dir}" "${branding_dir}"
wait_limit=120

cat >"${branding_dir}/custom.css" <<'EOF'
/* Hide deprecation banner and external notices */
.notice,
.alert,
[class*="banner"],
[class*="announcement"],
nav a[href*="github.com"] {
  display: none !important;
}
EOF

printf 'Starting File Browser (v%s)...\n' "${filebrowser_version}"

server_is_ready() {
  curl --connect-timeout 1 --max-time 1 --noproxy '*' --fail --silent \
    "http://127.0.0.1:${filebrowser_port}/health" >/dev/null
}

if command -v filebrowser >/dev/null 2>&1; then
  server_binary="$(command -v filebrowser)"
fi

old_token=
old_pid=
if [[ -f ${state_file} ]]; then
  read -r old_token old_pid <"${state_file}" || true
fi
if [[ ${old_token} == "${WORKSPACE_BOOT_TOKEN}" ]] \
  && [[ ${old_pid} =~ ^[1-9][0-9]*$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
  if server_is_ready; then
    write_ready_marker
    printf 'File Browser is already running (listening on http://127.0.0.1:%s).\n' "${filebrowser_port}"
    exit 0
  fi
  if [[ -r "/proc/${old_pid}/cmdline" ]] \
    && grep -Fq "${server_binary}" "/proc/${old_pid}/cmdline"; then
    kill "${old_pid}"
    wait "${old_pid}" 2>/dev/null || true
  else
    printf '%s\n' 'Refusing to stop a process not owned by this filebrowser launcher.' >&2
    exit 1
  fi
fi

if [[ ! -x ${server_binary} ]]; then
  archive="$(mktemp "${runtime_dir}/${artifact}.tar.gz.XXXXXX")"
  extraction="$(mktemp -d "${runtime_dir}/.filebrowser-install.XXXXXX")"
  cleanup_install() {
    rm -f -- "${archive}"
    rm -rf -- "${extraction}"
  }
  trap cleanup_install EXIT
  curl --silent --show-error --fail --location --retry 3 --retry-all-errors \
    --output "${archive}" \
    "https://github.com/filebrowser/filebrowser/releases/download/v${filebrowser_version}/${artifact}.tar.gz"
  archive_sha256="$(sha256sum "${archive}" | awk '{print $1}')"
  if [[ ${archive_sha256} != "${filebrowser_sha256}" ]]; then
    printf '%s\n' 'Downloaded filebrowser archive did not match its pinned SHA-256.' >&2
    exit 1
  fi
  tar -xzf "${archive}" -C "${extraction}"
  if [[ ! -x "${extraction}/filebrowser" ]]; then
    printf '%s\n' 'Downloaded filebrowser archive did not contain the expected binary.' >&2
    exit 1
  fi
  rm -rf -- "${install_prefix}"
  mkdir -p "${install_prefix}"
  mv -- "${extraction}/filebrowser" "${server_binary}"
  cleanup_install
  trap - EXIT
fi

# Ensure database is configured with noauth, non-admin user 'ops', and disabled disk usage graph
if [[ ! -f ${database_path} ]]; then
  "${server_binary}" config init \
    --database "${database_path}" \
    --auth.method=noauth \
    --branding.disableExternal \
    --branding.disableUsedPercentage \
    --branding.files="${branding_dir}" >/dev/null 2>&1 || true

  # Add initial non-admin user matching workspace session user
  initial_password="$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)"
  "${server_binary}" users add "${filebrowser_user}" "${initial_password}" \
    --database "${database_path}" \
    --perm.admin=false >/dev/null 2>&1 || true
fi

# Disallow search and recursive indexing from descending into /s3 mounts
"${server_binary}" rules add --path "/s3" --allow=false \
  --database "${database_path}" >/dev/null 2>&1 || true

nohup "${server_binary}" \
  --address 127.0.0.1 \
  --port "${filebrowser_port}" \
  --root /fs \
  --database "${database_path}" </dev/null >"${log_file}" 2>&1 &
server_pid=$!
printf '%s %s\n' "${WORKSPACE_BOOT_TOKEN}" "${server_pid}" >"${state_file}"

for ((attempt = 0; attempt < wait_limit; attempt++)); do
  if server_is_ready; then
    write_ready_marker
    printf 'File Browser is ready (listening on http://127.0.0.1:%s).\n' "${filebrowser_port}"
    exit 0
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    wait "${server_pid}" 2>/dev/null || true
    printf '%s\n' 'filebrowser exited before its health endpoint became ready.' >&2
    exit 1
  fi
  sleep 1
done
kill "${server_pid}" 2>/dev/null || true
wait "${server_pid}" 2>/dev/null || true
rm -f -- "${ready_file}"
printf '%s\n' 'filebrowser did not become ready before the startup deadline.' >&2
exit 1
