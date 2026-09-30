#!/usr/bin/env bash
# shellcheck disable=SC2310
# Launches the Zasper interactive notebook server on loopback for authenticated Coder application proxy access.

set -euo pipefail

: "${WORKSPACE_BOOT_TOKEN:?workspace boot token was not injected}"
: "${HOME:?workspace home was not injected}"

zasper_port=8048
wait_limit=120

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
ready_file="${runtime_dir}/workspace-zasper-ready"
state_file="${runtime_dir}/workspace-zasper.pid"
log_file="${runtime_dir}/workspace-zasper.log"

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

server_is_ready() {
  curl --connect-timeout 1 --max-time 1 --noproxy '*' --fail --silent \
    "http://127.0.0.1:${zasper_port}/api/health" >/dev/null
}

printf 'Starting Zasper notebook server...\n'

zasper=(zasper)
if ! command -v zasper >/dev/null 2>&1; then
  zasper=(mise exec -- zasper)
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
    printf 'Zasper is already running (listening on http://127.0.0.1:%s).\n' "${zasper_port}"
    exit 0
  fi
  kill "${old_pid}" 2>/dev/null || true
  wait "${old_pid}" 2>/dev/null || true
fi

export ZASPER_ACCESS_TOKEN="${ZASPER_ACCESS_TOKEN:-${WORKSPACE_BOOT_TOKEN}}"

zasper_config_dir="${HOME}/.zasper"
zasper_config_file="${zasper_config_dir}/config.json"
if [[ -L ${HOME} && ! -d ${HOME} ]]; then
  target_home=""
  target_home="$(readlink -f "${HOME}" 2>/dev/null || readlink "${HOME}" || true)"
  if [[ -n ${target_home} ]]; then
    mkdir -p "${target_home}"
  fi
fi
mkdir -p "${HOME}"
mkdir -p "${zasper_config_dir}"
if [[ ! -f ${zasper_config_file} ]]; then
  printf '{\n  "theme": "teal-dark"\n}\n' >"${zasper_config_file}"
elif ! grep -q '"theme"' "${zasper_config_file}" 2>/dev/null; then
  python3 -c "import json; p='${zasper_config_file}'; d=json.load(open(p)); d.setdefault('theme', 'teal-dark'); json.dump(d, open(p, 'w'), indent=2)" 2>/dev/null || true
fi

jupyter_kernel_dir="${HOME}/.local/share/jupyter/kernels/python3"
jupyter_kernel_file="${jupyter_kernel_dir}/kernel.json"
mkdir -p "${jupyter_kernel_dir}"
if [[ ! -f ${jupyter_kernel_file} ]]; then
  cat <<'EOF' >"${jupyter_kernel_file}"
{
  "argv": [
    "uv",
    "run",
    "--with",
    "ipykernel",
    "python",
    "-m",
    "ipykernel_launcher",
    "-f",
    "{connection_file}"
  ],
  "display_name": "Python 3",
  "language": "python"
}
EOF
fi

nohup "${zasper[@]}" \
  --cwd /fs \
  --host 0.0.0.0 \
  --port ":${zasper_port}" \
  --no-browser \
  --tracking=false </dev/null >"${log_file}" 2>&1 &
server_pid=$!
printf '%s %s\n' "${WORKSPACE_BOOT_TOKEN}" "${server_pid}" >"${state_file}"

for ((attempt = 0; attempt < wait_limit; attempt++)); do
  if server_is_ready; then
    write_ready_marker
    printf 'Zasper is ready (listening on http://127.0.0.1:%s).\n' "${zasper_port}"
    exit 0
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    wait "${server_pid}" 2>/dev/null || true
    printf '%s\n' 'Zasper exited before its health endpoint became ready.' >&2
    exit 1
  fi
  sleep 1
done

kill "${server_pid}" 2>/dev/null || true
wait "${server_pid}" 2>/dev/null || true
rm -f -- "${ready_file}"
printf '%s\n' 'Zasper did not become ready before the startup deadline.' >&2
exit 1
